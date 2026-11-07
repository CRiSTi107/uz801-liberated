"""
auth.py - authentication, sessions, CSRF and login rate-limiting.

Design (all stdlib):
  * Passwords   - salted PBKDF2-HMAC-SHA256 via ``hashlib.pbkdf2_hmac``, stored
                  in SQLite. Verified with ``hmac.compare_digest`` (constant time).
  * Server key  - a random HMAC secret generated on first run and persisted in
                  the ``meta`` table.
  * Sessions    - a random opaque token kept in the ``sessions`` table. The
                  cookie carries ``<token>.<hmac(secret, token)>`` so a tampered
                  or forged token is rejected before any DB lookup.
  * CSRF        - a random token bound to the session, handed to the client via
                  ``GET /api/session`` and required in the ``X-CSRF-Token``
                  header on every state-changing request.
  * Rate limit  - in-memory per-IP sliding window on failed logins.

Only the Python standard library is used.
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time

from logutil import get

log = get("cockpit.auth")

PBKDF2_ITERS = 200_000
PBKDF2_ALGO = "pbkdf2_hmac_sha256"
_HASH_NAME = "sha256"
_DKLEN = 32

COOKIE_NAME = "cockpit_session"


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------
def hash_password(password, salt=None, iters=PBKDF2_ITERS):
    """Return (salt_hex, hash_hex, iters) for a plaintext password."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode("utf-8"), salt, iters, _DKLEN)
    return salt.hex(), dk.hex(), iters


def verify_password(password, salt_hex, pwhash_hex, iters):
    """Constant-time verify a plaintext password against a stored hash."""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode("utf-8"), salt, iters, _DKLEN)
    return hmac.compare_digest(dk.hex(), pwhash_hex)


# ---------------------------------------------------------------------------
# Server secret (for signing session cookies)
# ---------------------------------------------------------------------------
def get_server_secret(db):
    """Fetch the persisted HMAC secret, creating one on first call."""
    secret = db.get_meta("server_secret")
    if not secret:
        secret = secrets.token_hex(32)
        db.set_meta("server_secret", secret)
        log.event(20, "auth.secret.created")
    return secret.encode("utf-8")


def _sign(secret, token):
    sig = hmac.new(secret, token.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


def make_cookie_value(secret, token):
    return "%s.%s" % (token, _sign(secret, token))


def parse_cookie_value(secret, value):
    """Return the token if the signature is valid, else None."""
    if not value or "." not in value:
        return None
    token, sig = value.rsplit(".", 1)
    expected = _sign(secret, token)
    if hmac.compare_digest(sig, expected):
        return token
    return None


# ---------------------------------------------------------------------------
# AuthManager - ties the DB, secret, sessions and rate-limiter together
# ---------------------------------------------------------------------------
class AuthManager:
    def __init__(self, db, session_ttl=86400, rate_max=5, rate_window=300,
                 rate_lock_seconds=300):
        self.db = db
        self.session_ttl = session_ttl
        self.secret = get_server_secret(db)
        # rate limiting state: ip -> list[timestamps of failures]
        self._fail_lock = threading.Lock()
        self._failures = {}
        self.rate_max = rate_max
        self.rate_window = rate_window
        self.rate_lock_seconds = rate_lock_seconds

    # --- first-run / setup ---------------------------------------------------
    def first_run(self):
        return not self.db.has_any_user()

    def setup_admin(self, username, password):
        salt_hex, pwhash_hex, iters = hash_password(password)
        self.db.upsert_user(username, salt_hex, pwhash_hex, iters, PBKDF2_ALGO)
        log.event(20, "auth.admin.setup", user=username)

    def set_password(self, username, password):
        self.setup_admin(username, password)

    def change_password(self, username, current, new):
        """Verify ``current`` then set ``new``. Returns True on success.

        Raises ValueError if the current password is wrong or ``new`` is too
        short, so the caller can map it to the right HTTP status.
        """
        user = self.db.get_user(username)
        if not user or not verify_password(
                current, user["salt"], user["pwhash"], user["iters"]):
            raise ValueError("current password is incorrect")
        if not new or len(new) < 8:
            raise ValueError("new password must be at least 8 characters")
        self.set_password(username, new)
        log.event(30, "auth.password.changed", user=username)
        return True

    # --- API tokens ----------------------------------------------------------
    @staticmethod
    def _hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_api_token(self, name):
        """Create a bearer token. Returns (id, plaintext). Store only the hash."""
        token_id = secrets.token_hex(8)
        plaintext = secrets.token_urlsafe(32)
        self.db.create_token(token_id, name, self._hash_token(plaintext))
        log.event(20, "auth.token.created", id=token_id, name=name)
        return token_id, plaintext

    def list_api_tokens(self):
        return self.db.list_tokens()

    def delete_api_token(self, token_id):
        row = self.db.get_token(token_id)
        if not row:
            return False
        self.db.delete_token(token_id)
        log.event(20, "auth.token.deleted", id=token_id)
        return True

    def validate_api_token(self, token):
        """Return the token row if ``token`` matches a stored hash, else None.

        Updates ``last_used`` on a hit.
        """
        if not token:
            return None
        row = self.db.get_token_by_hash(self._hash_token(token))
        if not row:
            return None
        self.db.touch_token(row["id"])
        return row

    # --- rate limiting -------------------------------------------------------
    def is_locked(self, ip):
        now = time.time()
        with self._fail_lock:
            fails = [t for t in self._failures.get(ip, []) if now - t < self.rate_window]
            self._failures[ip] = fails
            return len(fails) >= self.rate_max

    def record_failure(self, ip):
        now = time.time()
        with self._fail_lock:
            self._failures.setdefault(ip, []).append(now)

    def clear_failures(self, ip):
        with self._fail_lock:
            self._failures.pop(ip, None)

    # --- login / sessions ----------------------------------------------------
    def login(self, username, password):
        """Return a new session token on success, else None."""
        user = self.db.get_user(username)
        if not user:
            # Run a dummy hash to keep timing roughly constant for unknown users.
            hash_password(password)
            return None
        if not verify_password(password, user["salt"], user["pwhash"], user["iters"]):
            return None
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self.db.create_session(token, username, csrf, self.session_ttl)
        log.event(20, "auth.login.ok", user=username)
        return token

    def login_without_password(self, username):
        """Create a session for an already-authenticated user (e.g. post-setup).

        Returns a new session token. Does NOT verify a password - the caller
        must have established the identity (first-run setup).
        """
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self.db.create_session(token, username, csrf, self.session_ttl)
        return token

    def cookie_for(self, token):
        return make_cookie_value(self.secret, token)

    def session_from_cookie(self, cookie_value):
        """Validate a cookie and return the live session row, or None."""
        token = parse_cookie_value(self.secret, cookie_value)
        if not token:
            return None
        row = self.db.get_session(token)
        if not row:
            return None
        if row["expires"] < int(time.time()):
            self.db.delete_session(token)
            return None
        return row

    def logout(self, token):
        if token:
            self.db.delete_session(token)

    def logout_cookie(self, cookie_value):
        token = parse_cookie_value(self.secret, cookie_value)
        if token:
            self.db.delete_session(token)
