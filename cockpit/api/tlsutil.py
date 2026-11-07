"""
tlsutil.py - TLS helpers for cockpit-api.

``cryptography`` is banned on this target (it ships Rust/C extensions that are
painful to build against musl/armhf). So when a certificate is missing we
generate a self-signed one by shelling out to the ``openssl`` CLI, which is a
single ``apk add openssl`` on Alpine. Once the files exist we wrap the listening
socket with a stdlib ``ssl.SSLContext``.

Device note: ``apk add openssl`` is REQUIRED for first-run cert generation.
If you would rather generate the cert off-device, drop ``cockpit.crt`` and
``cockpit.key`` into the configured paths and set ``auto_generate: false``.

Only the Python standard library plus the external ``openssl`` binary are used.
"""

import os
import ssl
import subprocess

from logutil import get

log = get("cockpit.tls")


def ensure_cert(cfg):
    """Make sure cert+key exist, generating a self-signed pair if allowed.

    ``cfg`` is the ``tls`` sub-dict of the config. Returns ``(cert, key)`` paths.
    Raises RuntimeError if the files are missing and generation is disabled or
    fails.
    """
    cert = cfg["cert"]
    key = cfg["key"]

    if os.path.isfile(cert) and os.path.isfile(key):
        log.event(20, "tls.cert.found", cert=cert)
        return cert, key

    if not cfg.get("auto_generate", True):
        raise RuntimeError(
            "TLS cert/key missing and auto_generate is off: %s / %s" % (cert, key)
        )

    _generate_self_signed(cfg, cert, key)
    return cert, key


def _generate_self_signed(cfg, cert, key):
    """Run the openssl CLI to produce a self-signed cert+key."""
    openssl = cfg.get("openssl", "openssl")
    cn = cfg.get("cn", "uz801.local")
    days = str(int(cfg.get("days", 3650)))

    for path in (cert, key):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    # -nodes: no passphrase on the key (the service must start unattended).
    # subjectAltName lets browsers accept the device IP without a name warning.
    san = cfg.get("san", "IP:192.168.100.1,DNS:uz801.local")
    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", key, "-out", cert, "-days", days,
        "-subj", "/CN=%s" % cn,
        "-addext", "subjectAltName=%s" % san,
    ]
    log.event(20, "tls.cert.generate", cn=cn, days=days)
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except FileNotFoundError:
        raise RuntimeError(
            "openssl not found. Install it on the device with: apk add openssl"
        )
    except subprocess.CalledProcessError as exc:
        # Older openssl builds lack -addext; retry without the SAN extension.
        log.event(30, "tls.cert.addext_failed_retry", err=exc.stderr.decode("utf-8", "replace"))
        cmd_fallback = [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key, "-out", cert, "-days", days, "-subj", "/CN=%s" % cn,
        ]
        subprocess.run(cmd_fallback, check=True, capture_output=True)

    # Keep the private key readable only by the owner.
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    log.event(20, "tls.cert.generated", cert=cert, key=key)


def make_context(cert, key):
    """Build a hardened server SSLContext for the given cert+key."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    # Modern floor; TLS 1.2+ only.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!eNULL")
    except ssl.SSLError:
        pass
    return ctx
