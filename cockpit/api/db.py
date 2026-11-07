"""
db.py - SQLite persistence for cockpit-api.

Holds everything that must survive a restart:
  * ``meta``           - singleton key/value store (server HMAC secret, etc.)
  * ``users``          - admin account(s): salted PBKDF2 password hashes
  * ``sessions``       - active login sessions (token -> username, csrf, expiry)
  * ``sms``            - received + sent messages
  * ``call_log``       - dialled / received call history

SQLite is from the stdlib. ``http.server``'s ThreadingHTTPServer runs each
request in its own thread, so every access goes through a single connection
guarded by a re-entrant lock. That trades a little concurrency for correctness
and simplicity, which is the right call for a single-admin device UI.

Only the Python standard library is used.
"""

import os
import sqlite3
import threading
import time

from logutil import get

log = get("cockpit.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT UNIQUE NOT NULL,
    salt       TEXT NOT NULL,     -- hex
    pwhash     TEXT NOT NULL,     -- hex PBKDF2 output
    iters      INTEGER NOT NULL,
    algo       TEXT NOT NULL DEFAULT 'pbkdf2_hmac_sha256',
    created    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,  -- random session id (the signed cookie value)
    username   TEXT NOT NULL,
    csrf       TEXT NOT NULL,
    created    INTEGER NOT NULL,
    expires    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sms (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    box        TEXT NOT NULL,     -- 'inbox' | 'sent'
    address    TEXT,              -- peer MSISDN
    text       TEXT,              -- decoded body (unicode)
    ts         INTEGER NOT NULL,  -- unix epoch (best-effort)
    status     TEXT,              -- 'received' | 'sent' | 'failed' | 'unread'
    is_read    INTEGER NOT NULL DEFAULT 0,
    modem_index INTEGER,          -- index in modem storage, if known
    pdu        TEXT               -- raw PDU, if captured
);

CREATE TABLE IF NOT EXISTS call_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    number     TEXT,
    direction  TEXT NOT NULL,     -- 'out' | 'in'
    ts         INTEGER NOT NULL,
    status     TEXT               -- 'dialed' | 'answered' | 'hangup' | 'missed'
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id          TEXT PRIMARY KEY,  -- short public id (opaque to clients)
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL,     -- hex sha256 of the bearer token
    created     INTEGER NOT NULL,
    last_used   INTEGER            -- unix epoch of last successful use, or NULL
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False: we serialise access ourselves with _lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        log.event(20, "db.ready", path=path)

    # --- low-level helpers ---------------------------------------------------
    def execute(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query_one(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()

    def query_all(self, sql, params=()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    # --- meta ----------------------------------------------------------------
    def get_meta(self, key, default=None):
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --- users ---------------------------------------------------------------
    def has_any_user(self):
        row = self.query_one("SELECT COUNT(*) AS n FROM users")
        return bool(row and row["n"])

    def get_user(self, username):
        return self.query_one("SELECT * FROM users WHERE username=?", (username,))

    def upsert_user(self, username, salt_hex, pwhash_hex, iters, algo):
        self.execute(
            "INSERT INTO users(username,salt,pwhash,iters,algo,created) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "salt=excluded.salt, pwhash=excluded.pwhash, "
            "iters=excluded.iters, algo=excluded.algo",
            (username, salt_hex, pwhash_hex, iters, algo, int(time.time())),
        )

    # --- sessions ------------------------------------------------------------
    def create_session(self, token, username, csrf, ttl):
        now = int(time.time())
        self.execute(
            "INSERT INTO sessions(token,username,csrf,created,expires) "
            "VALUES(?,?,?,?,?)",
            (token, username, csrf, now, now + ttl),
        )

    def get_session(self, token):
        return self.query_one("SELECT * FROM sessions WHERE token=?", (token,))

    def delete_session(self, token):
        self.execute("DELETE FROM sessions WHERE token=?", (token,))

    def purge_expired_sessions(self):
        self.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))

    # --- sms -----------------------------------------------------------------
    def insert_sms(self, box, address, text, ts, status, modem_index=None,
                   pdu=None, is_read=0):
        cur = self.execute(
            "INSERT INTO sms(box,address,text,ts,status,is_read,modem_index,pdu) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (box, address, text, ts, status, is_read, modem_index, pdu),
        )
        return cur.lastrowid

    def list_sms(self, box):
        return self.query_all(
            "SELECT * FROM sms WHERE box=? ORDER BY ts DESC, id DESC", (box,)
        )

    def get_sms(self, sms_id):
        return self.query_one("SELECT * FROM sms WHERE id=?", (sms_id,))

    def delete_sms(self, sms_id):
        self.execute("DELETE FROM sms WHERE id=?", (sms_id,))

    def mark_sms_read(self, sms_id):
        self.execute("UPDATE sms SET is_read=1 WHERE id=?", (sms_id,))

    def sms_exists_by_index(self, modem_index):
        row = self.query_one(
            "SELECT id FROM sms WHERE modem_index=? AND box='inbox'", (modem_index,)
        )
        return row is not None

    # --- call log ------------------------------------------------------------
    def insert_call(self, number, direction, status):
        cur = self.execute(
            "INSERT INTO call_log(number,direction,ts,status) VALUES(?,?,?,?)",
            (number, direction, int(time.time()), status),
        )
        return cur.lastrowid

    def list_calls(self, limit=100):
        return self.query_all(
            "SELECT * FROM call_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        )

    # --- api tokens ----------------------------------------------------------
    def create_token(self, token_id, name, token_hash):
        self.execute(
            "INSERT INTO api_tokens(id,name,token_hash,created,last_used) "
            "VALUES(?,?,?,?,NULL)",
            (token_id, name, token_hash, int(time.time())),
        )

    def list_tokens(self):
        return self.query_all(
            "SELECT id,name,created,last_used FROM api_tokens "
            "ORDER BY created DESC, id DESC"
        )

    def get_token_by_hash(self, token_hash):
        return self.query_one(
            "SELECT * FROM api_tokens WHERE token_hash=?", (token_hash,)
        )

    def get_token(self, token_id):
        return self.query_one(
            "SELECT * FROM api_tokens WHERE id=?", (token_id,)
        )

    def touch_token(self, token_id):
        self.execute(
            "UPDATE api_tokens SET last_used=? WHERE id=?",
            (int(time.time()), token_id),
        )

    def delete_token(self, token_id):
        self.execute("DELETE FROM api_tokens WHERE id=?", (token_id,))

    def close(self):
        with self._lock:
            self._conn.close()
