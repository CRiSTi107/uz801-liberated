#!/usr/bin/env python3
"""
server.py - cockpit-api: a secure HTTPS management API for the UZ801 4G dongle.

Replaces the dongle's unauthenticated stock web API with an authenticated,
TLS-only REST service. Runs in an Alpine armhf chroot on the device (musl,
Python 3) and talks to the modem over /dev/smd11, with an allow-listed
passthrough to the stock /ajax API for WiFi/LAN functions.

Stdlib only (http.server, ssl, sqlite3, json, hashlib, hmac, secrets, ...).
See README.md for the full REST + WebSocket contract and device setup.

Run:
    python3 server.py --config config.json            # on the device
    python3 server.py --config config.json --mock      # offline dev on a PC
"""

import argparse
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs

import logutil
from db import Database
from auth import AuthManager, COOKIE_NAME
import modem as modemmod
import uzplatform as platformmod
from stockproxy import StockProxy, MockStockProxy, StockProxyError
import tlsutil
import audiobridge

log = logutil.get("cockpit.server")

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 443,
    "db_path": "/data/linux/cockpit/cockpit.db",
    "tls": {
        "cert": "/data/linux/cockpit/certs/cockpit.crt",
        "key": "/data/linux/cockpit/certs/cockpit.key",
        "openssl": "openssl",
        "auto_generate": True,
        "cn": "uz801.local",
        "san": "IP:192.168.100.1,DNS:uz801.local",
        "days": 3650,
        "enabled": True,
    },
    "modem": {"device": "/dev/smd11", "timeout": 8.0, "mock": False},
    "stock_api": {"base_url": "http://127.0.0.1:80", "timeout": 15},
    # Direct platform access (uzplatform.py) - reads the real system instead of
    # the stock :80 /ajax backdoor. When prefer_direct is true, endpoints use
    # the direct implementation and fall back to the stock proxy only if it
    # fails. Setting prefer_direct:false reverts to the stock-proxy behaviour.
    "platform": {
        "prefer_direct": True,
        "wan_iface": "rmnet0",
        "lan_iface": "br0",
        "hostapd_conf": "/data/misc/wifi/hostapd.conf",
        "hostapd_bin": "/system/bin/hostapd",
        "hostapd_entropy": "/data/misc/wifi/entropy.bin",
        "dnsmasq_leases": "/data/misc/dhcp/dnsmasq.leases",
        "getprop_bin": "/system/bin/getprop",
        # Per-mode AT command to set the preferred RAT (net mode). Verified
        # commands for this board go here; empty => fall back to stock 1005.
        # Example once verified: {"auto": "AT$QCSYSMODE=...", "4g": "..."}.
        "netmode_at": {},
        # AT command(s) to lock the LTE radio to specific band(s). Qualcomm-
        # modem-specific and unverified on this board => empty by default
        # (nothing is written). See config.example.json 'band_at' and modem.py
        # band_lock() for the template placeholders ({mask}/{mask_hex}).
        "band_at": {},
    },
    # Static SPA served at the web root (same origin as /api). Defaults to the
    # sibling cockpit/web/ directory for local dev; set explicitly on device.
    "web_root": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "web"),
    # Link-health probe folded into GET /api/status .link
    "link": {"target": "1.1.1.1", "poll_seconds": 10, "timeout": 1,
             "count": 1},
    "security": {
        "session_ttl": 86400,
        "login_rate_max": 5,
        "login_rate_window": 300,
        "hsts": True,
        "admin_user": "admin",
    },
    "sms_poll_seconds": 20,
    "audio": {"enabled": False, "host": "0.0.0.0", "port": 8089,
              "path": "/ws/audio", "rate": 8000},
    "system": {
        "reboot_cmd": ["reboot"],
        # Allow-listed services the /api/system/service endpoint may control.
        # Each maps a logical name to {action: argv}.
        "services": {
            "tailscale": {
                "start": ["tailscale", "up"],
                "stop": ["tailscale", "down"],
                "status": ["tailscale", "status"],
            },
            "adguard": {
                "start": ["rc-service", "adguardhome", "start"],
                "stop": ["rc-service", "adguardhome", "stop"],
                "status": ["rc-service", "adguardhome", "status"],
            }
        },
        "firewall_cmd": ["iptables", "-S"],
        "tailscale_status_cmd": ["tailscale", "status", "--json"],
        "adguard_check": {"port": 3000},
    },
    "log": {"level": "INFO", "file": None},
}


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ===========================================================================
# Application state shared across request threads
# ===========================================================================
class App:
    def __init__(self, cfg, mock=False, tls_enabled=True):
        self.cfg = cfg
        self.mock = mock
        self.tls_enabled = tls_enabled
        self.db = Database(cfg["db_path"])
        sec = cfg["security"]
        self.auth = AuthManager(
            self.db,
            session_ttl=sec.get("session_ttl", 86400),
            rate_max=sec.get("login_rate_max", 5),
            rate_window=sec.get("login_rate_window", 300),
        )
        self.admin_user = sec.get("admin_user", "admin")
        self.modem = modemmod.build_modem(cfg["modem"], mock=mock)
        proxy_cls = MockStockProxy if mock else StockProxy
        self.stock = proxy_cls(cfg["stock_api"]["base_url"],
                               cfg["stock_api"].get("timeout", 15))
        # Direct platform access replaces the stock :80 proxy where possible.
        plat_cfg = cfg.get("platform", {})
        self.platform = platformmod.build_platform(plat_cfg, mock=mock)
        self.prefer_direct = plat_cfg.get("prefer_direct", True)
        self._netmode_at = plat_cfg.get("netmode_at", {})
        self._band_at = plat_cfg.get("band_at", {})
        self._start_time = time.time()
        self._sms_poll_seconds = cfg.get("sms_poll_seconds", 20)
        self._stop = threading.Event()
        # Link-health cache, refreshed by a background poller.
        link_cfg = cfg.get("link", {})
        self._link_target = link_cfg.get("target", "1.1.1.1")
        self._link_poll_seconds = link_cfg.get("poll_seconds", 10)
        self._link = {"ping_ms": None, "up": False,
                      "target": self._link_target}
        self._link_lock = threading.Lock()

    # --- link health ---------------------------------------------------------
    def start_link_poller(self):
        if self._link_poll_seconds <= 0:
            return
        t = threading.Thread(target=self._link_poll_loop, daemon=True)
        t.start()

    def _link_poll_loop(self):
        self._refresh_link()
        while not self._stop.wait(self._link_poll_seconds):
            self._refresh_link()

    def _refresh_link(self):
        result = self._measure_link()
        with self._link_lock:
            self._link = result

    def _measure_link(self):
        target = self._link_target
        if self.mock:
            # Believable synthetic latency so the UI link indicator is live.
            ping_ms = round(18 + (time.time() * 7) % 25, 1)
            return {"ping_ms": ping_ms, "up": True, "target": target}
        link_cfg = self.cfg.get("link", {})
        cmd = ["ping", "-c", str(link_cfg.get("count", 1)),
               "-W", str(link_cfg.get("timeout", 1)), target]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=link_cfg.get("timeout", 1) + 3)
        except (OSError, subprocess.SubprocessError):
            return {"ping_ms": None, "up": False, "target": target}
        if proc.returncode != 0:
            return {"ping_ms": None, "up": False, "target": target}
        m = re.search(r"time[=<]\s*([\d.]+)\s*ms", proc.stdout)
        ping_ms = float(m.group(1)) if m else None
        return {"ping_ms": ping_ms, "up": ping_ms is not None,
                "target": target}

    def link_status(self):
        with self._link_lock:
            return dict(self._link)

    # --- SMS polling ---------------------------------------------------------
    def start_sms_poller(self):
        if self._sms_poll_seconds <= 0:
            return
        t = threading.Thread(target=self._sms_poll_loop, daemon=True)
        t.start()

    def _sms_poll_loop(self):
        # Prime the modem: PDU indications, store-and-notify.
        try:
            self.modem.sms_set_cnmi()
        except Exception as exc:  # noqa: BLE001 - best effort
            log.event(30, "sms.cnmi_failed", err=str(exc))
        while not self._stop.wait(self._sms_poll_seconds):
            try:
                self._sync_inbox()
            except Exception as exc:  # noqa: BLE001
                log.event(30, "sms.poll_failed", err=str(exc))

    def _sync_inbox(self):
        messages = self.modem.sms_list_pdu(stat=4)  # ALL
        for msg in messages:
            idx = msg.get("index")
            if idx is None or self.db.sms_exists_by_index(idx):
                continue
            ts = _parse_scts_epoch(msg.get("timestamp"))
            self.db.insert_sms(
                box="inbox",
                address=msg.get("sender"),
                text=msg.get("text"),
                ts=ts,
                status="received",
                modem_index=idx,
                pdu=msg.get("pdu"),
                is_read=0,
            )
            log.event(20, "sms.received", index=idx, sender=msg.get("sender"))

    def uptime(self):
        return int(time.time() - self._start_time)

    def shutdown(self):
        self._stop.set()
        self.db.close()


def _parse_scts_epoch(scts):
    if not scts:
        return int(time.time())
    try:
        t = time.strptime(scts, "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(t))
    except (ValueError, TypeError):
        return int(time.time())


# ===========================================================================
# Request handler
# ===========================================================================
class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "cockpit-api/1.0"
    protocol_version = "HTTP/1.1"

    # app is attached to the server instance.
    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        log.event(10, "http", peer=self.address_string(), line=(fmt % args))

    # --- dispatch ------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        # Always fully drain the request body FIRST. On keep-alive (HTTP/1.1)
        # connections, a body that the matched route ignores -- or an early
        # error return (401/404/...) -- would otherwise stay in the socket and
        # corrupt the framing of the NEXT request (seen as
        # "Unsupported method ('{}GET')").
        try:
            _blen = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            _blen = 0
        if _blen < 0:
            _blen = 0
        if _blen > 1_000_000:
            _rem = _blen
            while _rem > 0:
                _chunk = self.rfile.read(min(_rem, 65536))
                if not _chunk:
                    break
                _rem -= len(_chunk)
            self._body = b""
            self._send_json(413, {"error": "request too large"})
            return
        self._body = self.rfile.read(_blen) if _blen else b""
        try:
            route = self._match(method, path)
            if route is None:
                if method == "GET" and not path.startswith("/api") \
                        and path != "/ws/audio":
                    if self._serve_static(parsed.path):
                        return
                raise ApiError(404, "no such endpoint: %s %s" % (method, path))
            handler_fn, needs_auth, is_write, args = route

            session = self._current_session()
            if needs_auth:
                if not session:
                    raise ApiError(401, "authentication required")
                # Cookie sessions require a CSRF token on writes; bearer-token
                # (API) auth is not cookie-based so CSRF does not apply.
                if is_write and not session.get("via_token"):
                    self._check_csrf(session)
            result = handler_fn(query, args, session)
            self._send_json(200, result)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except StockProxyError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            log.event(40, "http.unhandled", err=str(exc), path=path)
            self._send_json(500, {"error": "internal error"})

    # Routing table: (method, regex) -> (fn, needs_auth, is_write)
    def _match(self, method, path):
        for m, pattern, fn_name, needs_auth, is_write in ROUTES:
            if m != method:
                continue
            match = re.fullmatch(pattern, path)
            if match:
                return (getattr(self, fn_name), needs_auth, is_write,
                        match.groups())
        return None

    # --- helpers -------------------------------------------------------------
    def _read_json(self):
        raw = getattr(self, "_body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "invalid JSON body")
        if not isinstance(data, dict):
            raise ApiError(400, "JSON body must be an object")
        return data

    def _cookies(self):
        jar = SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            try:
                jar.load(raw)
            except Exception:  # noqa: BLE001
                pass
        return jar

    def _current_session(self):
        jar = self._cookies()
        morsel = jar.get(COOKIE_NAME)
        if morsel:
            row = self.app.auth.session_from_cookie(morsel.value)
            if row:
                return {"username": row["username"], "csrf": row["csrf"],
                        "via_token": False}
        # Fall back to a bearer API token: "Authorization: Bearer <token>".
        auth_hdr = self.headers.get("Authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            tok = auth_hdr[7:].strip()
            row = self.app.auth.validate_api_token(tok)
            if row:
                return {"username": self.app.admin_user, "csrf": None,
                        "via_token": True, "token_id": row["id"]}
        return None

    def _check_csrf(self, session):
        # Canonical header is X-CSRF (per the SPA contract); accept the older
        # X-CSRF-Token spelling too for robustness.
        token = (self.headers.get("X-CSRF")
                 or self.headers.get("X-CSRF-Token") or "")
        if not token or token != session["csrf"]:
            raise ApiError(403, "missing or invalid CSRF token")

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.app.cfg["security"].get("hsts", True):
            self.send_header("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
        for name, value in getattr(self, "_extra_headers", []):
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _add_header(self, name, value):
        if not hasattr(self, "_extra_headers"):
            self._extra_headers = []
        self._extra_headers.append((name, value))

    # --- static SPA serving --------------------------------------------------
    _STATIC_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2",
        ".map": "application/json",
    }

    def _serve_static(self, url_path):
        """Serve the SPA from cfg['web_root']. Returns True if handled."""
        web_root = self.app.cfg.get("web_root")
        if not web_root or not os.path.isdir(web_root):
            return False
        root = os.path.realpath(web_root)
        rel = url_path.lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        target = os.path.realpath(os.path.join(root, rel))
        # Path-traversal guard: resolved path must stay inside the web root.
        if target != root and not target.startswith(root + os.sep):
            raise ApiError(403, "forbidden")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            return False
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            return False
        ext = os.path.splitext(target)[1].lower()
        ctype = self._STATIC_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if self.app.cfg["security"].get("hsts", True) and self.app.tls_enabled:
            self.send_header("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def _set_session_cookie(self, cookie_value, max_age):
        attrs = [
            "%s=%s" % (COOKIE_NAME, cookie_value),
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=%d" % max_age,
        ]
        # Secure is only meaningful over TLS; dropping it under --no-tls dev
        # lets a plain-HTTP client (curl, local preview) keep its cookie.
        if self.app.tls_enabled:
            attrs.insert(3, "Secure")
        self._add_header("Set-Cookie", "; ".join(attrs))

    def _clear_session_cookie(self):
        secure = "Secure; " if self.app.tls_enabled else ""
        self._add_header(
            "Set-Cookie",
            "%s=; Path=/; HttpOnly; %sSameSite=Strict; Max-Age=0"
            % (COOKIE_NAME, secure),
        )

    # =======================================================================
    # Endpoint handlers
    # =======================================================================
    # --- auth / session ----------------------------------------------------
    def _audio_info(self):
        audio = self.app.cfg.get("audio", {})
        return {
            "enabled": bool(audio.get("enabled")),
            "port": audio.get("port", 8089),
            "path": audio.get("path", "/ws/audio"),
            "rate": audio.get("rate", 8000),
        }

    def ep_session(self, query, args, session):
        return {
            "authenticated": bool(session),
            "first_run": self.app.auth.first_run(),
            "csrf": session["csrf"] if session else None,
            "user": session["username"] if session else None,
            "audio": self._audio_info(),
        }

    def _begin_session(self, username):
        """Create a session + set the cookie. Returns the fresh csrf."""
        token = self.app.auth.login_without_password(username)
        self._set_session_cookie(self.app.auth.cookie_for(token),
                                 self.app.auth.session_ttl)
        return self.app.db.get_session(token)["csrf"]

    def ep_setup(self, query, args, session):
        if not self.app.auth.first_run():
            raise ApiError(409, "already set up")
        body = self._read_json()
        username = (body.get("username") or self.app.admin_user).strip() \
            or self.app.admin_user
        password = body.get("password")
        if not password or len(password) < 8:
            raise ApiError(400, "password must be at least 8 characters")
        self.app.admin_user = username
        self.app.auth.setup_admin(username, password)
        # Setup logs the new admin straight in (contract: returns csrf + user).
        csrf = self._begin_session(username)
        return {"csrf": csrf, "user": username}

    def ep_login(self, query, args, session):
        ip = self._client_ip()
        if self.app.auth.is_locked(ip):
            raise ApiError(429, "too many attempts; try again later")
        body = self._read_json()
        username = body.get("username", "")
        password = body.get("password", "")
        token = self.app.auth.login(username, password)
        if not token:
            self.app.auth.record_failure(ip)
            log.event(30, "auth.login.fail", user=username, ip=ip)
            raise ApiError(401, "invalid credentials")
        self.app.auth.clear_failures(ip)
        self._set_session_cookie(self.app.auth.cookie_for(token),
                                 self.app.auth.session_ttl)
        row = self.app.db.get_session(token)
        return {"csrf": row["csrf"], "user": username}

    def ep_logout(self, query, args, session):
        jar = self._cookies()
        morsel = jar.get(COOKIE_NAME)
        if morsel:
            self.app.auth.logout_cookie(morsel.value)
        self._clear_session_cookie()
        return {"ok": True}

    # --- status ------------------------------------------------------------
    def ep_status(self, query, args, session):
        m = self.app.modem
        sig = m.signal()
        reg = m.registration()
        data = m.data_status()
        # Traffic counters: DIRECT from /proc/net/dev (rmnet0 WAN) preferred;
        # rx = download, tx = upload. Fall back to stock funcNo 1003.
        if self.app.prefer_direct:
            try:
                t = self.app.platform.traffic()
                wan = (t or {}).get("wan") or {}
                if wan.get("rx_bytes") is not None:
                    data["down_bytes"] = wan.get("rx_bytes")
                    data["up_bytes"] = wan.get("tx_bytes")
            except Exception:  # noqa: BLE001 - direct is best-effort
                pass
        if data.get("up_bytes") is None and data.get("down_bytes") is None:
            # Enrich up/down bytes from the stock traffic funcNo (1003).
            try:
                traffic = self.app.stock.forward({"funcNo": 1003})
                res = (traffic.get("results") or [{}])[0]
                data["up_bytes"] = _to_int(res.get("up") or res.get("tx")
                                           or res.get("upload"))
                data["down_bytes"] = _to_int(res.get("down") or res.get("rx")
                                             or res.get("download"))
            except Exception:  # noqa: BLE001 - optional enrichment
                pass
        # Direct IP fallback for the WAN if AT+CGPADDR did not report one.
        if not data.get("ip") and self.app.prefer_direct:
            try:
                addrs = self.app.platform.addresses()
                data["ip"] = (addrs.get("wan") or {}).get("ip") or data.get("ip")
            except Exception:  # noqa: BLE001
                pass

        usable, hint = modemmod.band_usable(sig.get("band"))
        return {
            "signal": {
                "rsrp": sig.get("rsrp"),
                "rsrq": sig.get("rsrq"),
                "sinr": sig.get("sinr"),
                "csq": sig.get("csq"),
            },
            "radio": {
                "band": sig.get("band"),
                "earfcn": sig.get("earfcn"),
                "pci": sig.get("pci"),
                "cell_id": reg.get("cell_id"),
                "band_usable": usable,
                "band_hint": hint,
            },
            "registration": {
                "state": reg.get("state"),
                "registered": reg.get("registered", False),
                "operator": reg.get("operator"),
                "rat": reg.get("rat"),
            },
            "data": {
                "connected": data.get("connected", False),
                "ip": data.get("ip"),
                "up_bytes": data.get("up_bytes"),
                "down_bytes": data.get("down_bytes"),
            },
            "uptime_s": self.app.uptime(),
            "link": self.app.link_status(),
        }

    # --- SIM ---------------------------------------------------------------
    def ep_sim(self, query, args, session):
        m = self.app.modem
        out = {
            "imei": m.imei(),
            "imsi": m.imsi(),
            "iccid": m.iccid(),
            "sim_status": m.cpin(),
            "msisdn": m.msisdn(),
        }
        # Device model / firmware: DIRECT via getprop instead of stock
        # funcNo 1029/3002. Best-effort; omitted if getprop is unavailable.
        if self.app.prefer_direct:
            try:
                info = self.app.platform.device_info()
                out["model"] = info.get("ro.product.model")
                out["manufacturer"] = info.get("ro.product.manufacturer")
                out["firmware"] = (info.get("ro.build.display.id")
                                   or info.get("ro.build.version.release"))
                out["android"] = info.get("ro.build.version.release")
            except Exception:  # noqa: BLE001
                pass
        return out

    def ep_sim_imei(self, query, args, session):
        body = self._read_json()
        imei = str(body.get("imei", "")).strip()
        if body.get("confirm") is not True:
            raise ApiError(400, "IMEI write requires confirm:true (dangerous)")
        try:
            res = self.app.modem.write_imei(imei)
        except ValueError as exc:
            raise ApiError(400, str(exc))
        log.event(30, "sim.imei.write", imei=imei, ok=res.ok)
        return {"ok": res.ok, "status": res.status, "raw": res.raw}

    # --- network / APN -----------------------------------------------------
    def ep_network(self, query, args, session):
        m = self.app.modem
        out = {
            "reg": m.registration(),
            "contexts": m.contexts(),
            "data": m.data_status(),
        }
        # Net mode: DIRECT from the modem RAT (AT), fall back to stock 1005.
        netmode = None
        if self.app.prefer_direct:
            try:
                netmode = m.net_mode()
            except Exception:  # noqa: BLE001
                netmode = None
        if netmode is None:
            try:
                netmode = self.app.stock.forward({"funcNo": 1005})
            except Exception:  # noqa: BLE001
                netmode = None
        out["netmode"] = netmode
        # DNS + interface addresses: DIRECT from the system.
        if self.app.prefer_direct:
            try:
                out["dns"] = self.app.platform.dns_servers()
            except Exception:  # noqa: BLE001
                out["dns"] = []
            try:
                out["addresses"] = self.app.platform.addresses()
            except Exception:  # noqa: BLE001
                out["addresses"] = {}
        return out

    def ep_network_apn_get(self, query, args, session):
        """GET current APN profile as {apn, user, pwd, auth}."""
        ctxs = self.app.modem.contexts()
        apn = ctxs[0]["apn"] if ctxs else ""
        out = {"apn": apn, "user": "", "pwd": "", "auth": "0"}
        # Enrich from the stock profile where available (funcNo 1017 region).
        try:
            stock = self.app.stock.forward({"funcNo": 1015})
            if isinstance(stock, dict):
                out["apn"] = stock.get("apn", out["apn"]) or out["apn"]
                out["user"] = stock.get("user", out["user"]) or out["user"]
                out["pwd"] = stock.get("pwd", out["pwd"]) or out["pwd"]
                if stock.get("auth") is not None:
                    out["auth"] = str(stock.get("auth"))
        except Exception:  # noqa: BLE001 - stock enrichment is best-effort
            pass
        return out

    def ep_network_apn(self, query, args, session):
        body = self._read_json()
        apn = body.get("apn")
        if not apn:
            raise ApiError(400, "apn is required")
        user = body.get("user", "")
        pwd = body.get("pwd", "")
        auth = str(body.get("auth", "0"))
        # Primary path: set the PDP context on the modem (works offline/mock).
        res = self.app.modem.set_apn(apn)
        # Best-effort stock path (proven on this firmware: funcNo 1017).
        stock_result = None
        try:
            stock_result = self.app.stock.forward({
                "funcNo": 1017, "no": "1", "name": body.get("name", apn),
                "apn": apn, "user": user, "pwd": pwd, "auth": auth,
            })
        except Exception:  # noqa: BLE001
            stock_result = None
        return {"ok": res.ok, "at_status": res.status, "stock": stock_result}

    def ep_network_netmode(self, query, args, session):
        body = self._read_json()
        mode = body.get("mode")
        if mode is None:
            raise ApiError(400, "mode is required")
        # DIRECT: set the preferred RAT via a configured AT template. Falls
        # back to stock funcNo 1005 if no verified AT command is configured.
        if self.app.prefer_direct:
            try:
                res = self.app.modem.set_net_mode(mode, self.app._netmode_at)
            except Exception:  # noqa: BLE001
                res = None
            if res is not None:
                return {"ok": res.ok, "at_status": res.status,
                        "source": "at"}
        # funcNo 1005 param is net_mode on this firmware.
        result = self.app.stock.forward({"funcNo": 1005, "net_mode": str(mode)})
        return {"ok": True, "stock": result, "source": "stock"}

    # --- band lock ---------------------------------------------------------
    def ep_band_get(self, query, args, session):
        """Report supported bands, the live serving band, and the band-lock
        config state (so the UI knows whether locking is wired for this modem).
        """
        m = self.app.modem
        try:
            sig = m.signal()
        except Exception:  # noqa: BLE001
            sig = {}
        serving = sig.get("band")
        usable, hint = modemmod.band_usable(serving)
        status = m.band_status(self.app._band_at)
        return {
            "supported": modemmod.LTE_BANDS_SUPPORTED,
            "serving": {
                "band": serving,
                "earfcn": sig.get("earfcn"),
                "rsrp": sig.get("rsrp"),
                "usable": usable,
                "hint": hint,
            },
            # cells currently visible, each tagged usable/not, to guide choice
            "cells": [
                dict(c, usable=modemmod.band_usable(c.get("band"))[0])
                for c in sig.get("cells", [])
            ],
            "configured": bool(self.app._band_at),
            "current": status,
        }

    def ep_band_set(self, query, args, session):
        """Lock LTE to band(s), or 'auto' for all supported bands.

        Body: {"bands": "auto" | "B3" | ["B1","B3"]}.
        """
        body = self._read_json()
        bands = body.get("bands")
        if bands is None:
            raise ApiError(400, "bands is required ('auto', a label, or a list)")
        res = self.app.modem.band_lock(bands, self.app._band_at)
        if res["source"] == "unconfigured":
            raise ApiError(
                501,
                "band-lock AT command is not configured for this modem - set "
                "platform.band_at in config.json (the exact command is "
                "Qualcomm-modem-specific; see config.example.json). Requested "
                "bands=%s mask=0x%s." % (res["bands"], res["mask_hex"]))
        if not res["ok"]:
            raise ApiError(502, "modem rejected band set: %s" % res["status"])
        return res

    # --- SMS ---------------------------------------------------------------
    def ep_sms_list(self, query, args, session):
        box = (query.get("box") or ["inbox"])[0]
        if box not in ("inbox", "sent"):
            raise ApiError(400, "box must be inbox or sent")
        rows = self.app.db.list_sms(box)
        return {"box": box, "messages": [_sms_row(r) for r in rows]}

    def ep_sms_send(self, query, args, session):
        body = self._read_json()
        to = body.get("to")
        text = body.get("text")
        if not to or text is None:
            raise ApiError(400, "to and text are required")
        result = self.app.modem.sms_send(to, text)
        row_id = self.app.db.insert_sms(
            box="sent", address=to, text=text, ts=int(time.time()),
            status="sent" if result["ok"] else "failed",
            modem_index=None, pdu=None, is_read=1,
        )
        if not result["ok"]:
            # Not-registered is a precondition failure (the caller can't fix it
            # by retrying) - distinguish it from a genuine modem/gateway error.
            if result.get("registered") is False:
                raise ApiError(409, result.get("error") or "modem not registered")
            raise ApiError(502, "send failed: %s" % result.get("error"))
        return {"id": str(row_id), "references": result["references"],
                "parts": result["parts"]}

    def ep_sms_delete(self, query, args, session):
        sms_id = int(args[0])
        row = self.app.db.get_sms(sms_id)
        if not row:
            raise ApiError(404, "no such message")
        # Delete from the modem storage too, if we know its index.
        if row["modem_index"] is not None:
            try:
                self.app.modem.sms_delete(row["modem_index"])
            except Exception as exc:  # noqa: BLE001
                log.event(30, "sms.modem_delete_failed", err=str(exc))
        self.app.db.delete_sms(sms_id)
        return {"ok": True, "id": sms_id}

    # --- calls -------------------------------------------------------------
    def ep_call_status(self, query, args, session):
        return {"calls": self.app.modem.call_status()}

    def ep_call_dial(self, query, args, session):
        body = self._read_json()
        number = body.get("number")
        if not number:
            raise ApiError(400, "number is required")
        res = self.app.modem.dial(number)
        self.app.db.insert_call(number, "out",
                                "dialed" if res.ok else "failed")
        if not res.ok:
            raise ApiError(502, "dial failed: %s" % res.status)
        # No call id is known until CLCC reports it; the SPA re-polls /call.
        return {"ok": True, "id": None}

    def ep_call_answer(self, query, args, session):
        res = self.app.modem.answer()
        self.app.db.insert_call(None, "in", "answered")
        if not res.ok:
            raise ApiError(502, "answer failed: %s" % res.status)
        return {"ok": True}

    def ep_call_hangup(self, query, args, session):
        res = self.app.modem.hangup()
        self.app.db.insert_call(None, "out", "hangup")
        return {"ok": res.ok, "status": res.status}

    # --- wifi (direct hostapd.conf; stock proxy fallback) ------------------
    def ep_wifi(self, query, args, session):
        # DIRECT: read /data/misc/wifi/hostapd.conf.
        if self.app.prefer_direct:
            try:
                d = self.app.platform.wifi_get()
            except Exception as exc:  # noqa: BLE001
                log.event(30, "wifi.direct_get_failed", err=str(exc))
                d = None
            if d is not None:
                return {
                    "ssid": d.get("ssid", ""),
                    "password": d.get("password", ""),
                    "encryption": d.get("encryption", "wpa2"),
                    "max_clients": d.get("max_clients"),
                    "channel": d.get("channel"),
                }
        # Fallback: stock funcNo 1006.
        stock = self.app.stock.forward({"funcNo": 1006})
        if not isinstance(stock, dict):
            stock = {}
        return {
            "ssid": stock.get("ssid", ""),
            "password": stock.get("pwd", stock.get("password", "")),
            "encryption": _encryp_to_api(stock.get("encryp_type")),
            "max_clients": _to_int(stock.get("maxSta",
                                             stock.get("max_clients"))),
        }

    def ep_wifi_set(self, query, args, session):
        body = self._read_json()
        encryption = str(body.get("encryption", "wpa2")).lower()
        # DIRECT: rewrite hostapd.conf + restart hostapd.
        if self.app.prefer_direct:
            try:
                res = self.app.platform.wifi_set(
                    ssid=body.get("ssid"),
                    password=body.get("password"),
                    encryption=body.get("encryption"),
                    max_clients=body.get("max_clients"),
                    channel=body.get("channel"),
                )
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.event(40, "wifi.direct_set_failed", err=str(exc))
                res = None
            if res and res.get("ok"):
                out = {"ok": True, "encryption": res.get("encryption"),
                       "restart": res.get("restart"), "source": "direct"}
                if res.get("warn"):
                    out["warn"] = res["warn"]
                return out
        # Fallback: stock funcNo 1007.
        encryp_type, note = _api_to_encryp(encryption)
        payload = {"funcNo": 1007, "encryp_type": encryp_type}
        if "ssid" in body:
            payload["ssid"] = body["ssid"]
        if "password" in body:
            payload["pwd"] = body["password"]
        if "max_clients" in body:
            payload["maxSta"] = body["max_clients"]
        self.app.stock.forward(payload)
        out = {"ok": True, "encryption": _encryp_to_api(encryp_type),
               "encryp_type": encryp_type, "source": "stock"}
        if note:
            out["warn"] = note
        return out

    def ep_wifi_clients(self, query, args, session):
        # DIRECT: dnsmasq leases + /proc/net/arp on the LAN bridge.
        if self.app.prefer_direct:
            try:
                clients = self.app.platform.wifi_clients()
                return {"clients": clients, "source": "direct"}
            except Exception as exc:  # noqa: BLE001
                log.event(30, "wifi.direct_clients_failed", err=str(exc))
        # Fallback: stock funcNo 1010.
        stock = self.app.stock.forward({"funcNo": 1010})
        raw = stock.get("clients", stock) if isinstance(stock, dict) else stock
        clients = []
        for c in (raw or []):
            if not isinstance(c, dict):
                continue
            clients.append({
                "hostname": c.get("hostname") or c.get("name"),
                "ip": c.get("ip") or c.get("ipaddr"),
                "mac": c.get("mac") or c.get("macaddr"),
            })
        return {"clients": clients, "source": "stock"}

    def ep_wifi_macfilter_get(self, query, args, session):
        # DIRECT: hostapd macaddr_acl + accept/deny mac files.
        if self.app.prefer_direct:
            try:
                mf = self.app.platform.macfilter_get()
            except Exception as exc:  # noqa: BLE001
                log.event(30, "wifi.macfilter_get_failed", err=str(exc))
                mf = None
            if mf is not None:
                mf["source"] = "direct"
                return mf
        # Fallback: stock funcNo 1052 (MAC filter list on this firmware).
        try:
            stock = self.app.stock.forward({"funcNo": 1052})
        except StockProxyError:
            raise
        return {"source": "stock", "stock": stock}

    def ep_wifi_macfilter_set(self, query, args, session):
        body = self._read_json()
        mode = body.get("mode")
        if mode is not None and mode not in ("disabled", "accept", "deny"):
            raise ApiError(400, "mode must be disabled|accept|deny")
        accept = body.get("accept")
        deny = body.get("deny")
        if self.app.prefer_direct:
            try:
                res = self.app.platform.macfilter_set(
                    mode=mode, accept=accept, deny=deny)
            except Exception as exc:  # noqa: BLE001
                log.event(40, "wifi.macfilter_set_failed", err=str(exc))
                res = None
            if res and res.get("ok"):
                res["source"] = "direct"
                return res
        # Fallback: stock funcNo 1054 (set MAC filter on this firmware).
        stock = self.app.stock.forward({
            "funcNo": 1054, "mode": mode,
            "accept": accept or [], "deny": deny or [],
        })
        return {"ok": True, "source": "stock", "stock": stock}

    # --- system ------------------------------------------------------------
    def ep_system(self, query, args, session):
        info = system_info(self.app.cfg["system"], mock=self.app.mock)
        # Device identity: DIRECT via getprop (replaces stock funcNo 3002).
        try:
            info["device"] = self.app.platform.device_info()
        except Exception:  # noqa: BLE001
            info["device"] = {}
        return info

    def ep_system_reboot(self, query, args, session):
        if self.app.mock:
            return {"ok": True, "mock": True}
        cmd = self.app.cfg["system"].get("reboot_cmd", ["reboot"])
        log.event(30, "system.reboot")
        try:
            subprocess.Popen(cmd)
        except OSError as exc:
            raise ApiError(500, "reboot failed: %s" % exc)
        return {"ok": True}

    def ep_system_service(self, query, args, session):
        body = self._read_json()
        name = body.get("name")
        action = body.get("action")
        services = self.app.cfg["system"].get("services", {})
        if name not in services:
            raise ApiError(400, "unknown service: %s" % name)
        if action not in services[name]:
            raise ApiError(400, "unknown action: %s" % action)
        if self.app.mock:
            return {"ok": True, "mock": True, "name": name, "action": action}
        cmd = services[name][action]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=30,
                                  text=True)
            return {"ok": proc.returncode == 0, "code": proc.returncode,
                    "stdout": proc.stdout, "stderr": proc.stderr}
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiError(500, "service action failed: %s" % exc)

    # --- settings / password ------------------------------------------------
    def ep_settings_password(self, query, args, session):
        body = self._read_json()
        current = body.get("current", "")
        new = body.get("new", "")
        try:
            self.app.auth.change_password(session["username"], current, new)
        except ValueError as exc:
            # Wrong current password -> 403; weak new password -> 400.
            status = 403 if "incorrect" in str(exc) else 400
            raise ApiError(status, str(exc))
        return {"ok": True}

    # --- API tokens ----------------------------------------------------------
    def ep_tokens_list(self, query, args, session):
        rows = self.app.auth.list_api_tokens()
        return {"tokens": [
            {"id": r["id"], "name": r["name"], "created": r["created"],
             "last_used": r["last_used"]} for r in rows
        ]}

    def ep_tokens_create(self, query, args, session):
        body = self._read_json()
        name = (body.get("name") or "").strip()
        if not name:
            raise ApiError(400, "token name is required")
        token_id, plaintext = self.app.auth.create_api_token(name)
        # The plaintext token is returned exactly once, never stored.
        return {"id": token_id, "token": plaintext}

    def ep_tokens_delete(self, query, args, session):
        token_id = args[0]
        if not self.app.auth.delete_api_token(token_id):
            raise ApiError(404, "no such token")
        return {"ok": True}

    # --- stock allow-listed passthrough ------------------------------------
    def ep_ajax(self, query, args, session):
        body = self._read_json()
        danger = bool(body.pop("danger", False))
        result = self.app.stock.forward(body, danger=danger)
        return result


# ===========================================================================
# Route table (method, path-regex, handler-name, needs_auth, is_write)
# ===========================================================================
ROUTES = [
    ("GET",    r"/api/session",          "ep_session",        False, False),
    ("POST",   r"/api/setup",            "ep_setup",          False, False),
    ("POST",   r"/api/login",            "ep_login",          False, False),
    ("POST",   r"/api/logout",           "ep_logout",         True,  False),

    ("GET",    r"/api/status",           "ep_status",         True,  False),

    ("GET",    r"/api/sim",              "ep_sim",            True,  False),
    ("POST",   r"/api/sim/imei",         "ep_sim_imei",       True,  True),

    ("GET",    r"/api/network",          "ep_network",        True,  False),
    ("GET",    r"/api/network/apn",      "ep_network_apn_get", True, False),
    ("POST",   r"/api/network/apn",      "ep_network_apn",    True,  True),
    ("POST",   r"/api/network/netmode",  "ep_network_netmode", True, True),
    ("GET",    r"/api/band",             "ep_band_get",       True,  False),
    ("POST",   r"/api/band",             "ep_band_set",       True,  True),

    ("GET",    r"/api/sms",              "ep_sms_list",       True,  False),
    ("POST",   r"/api/sms",              "ep_sms_send",       True,  True),
    ("DELETE", r"/api/sms/(\d+)",        "ep_sms_delete",     True,  True),

    ("GET",    r"/api/call",             "ep_call_status",    True,  False),
    ("POST",   r"/api/call",             "ep_call_dial",      True,  True),
    ("POST",   r"/api/call/answer",      "ep_call_answer",    True,  True),
    ("POST",   r"/api/call/hangup",      "ep_call_hangup",    True,  True),

    ("GET",    r"/api/wifi",             "ep_wifi",           True,  False),
    ("POST",   r"/api/wifi",             "ep_wifi_set",       True,  True),
    ("GET",    r"/api/wifi/clients",     "ep_wifi_clients",   True,  False),
    ("GET",    r"/api/wifi/macfilter",   "ep_wifi_macfilter_get", True, False),
    ("POST",   r"/api/wifi/macfilter",   "ep_wifi_macfilter_set", True, True),

    ("GET",    r"/api/system",           "ep_system",         True,  False),
    ("POST",   r"/api/system/reboot",    "ep_system_reboot",  True,  True),
    ("POST",   r"/api/system/service",   "ep_system_service", True,  True),

    ("POST",   r"/api/settings/password", "ep_settings_password", True, True),

    ("GET",    r"/api/tokens",           "ep_tokens_list",    True,  False),
    ("POST",   r"/api/tokens",           "ep_tokens_create",  True,  True),
    ("DELETE", r"/api/tokens/([^/]+)",   "ep_tokens_delete",  True,  True),

    ("POST",   r"/api/ajax",             "ep_ajax",           True,  True),
]


# ===========================================================================
# System info helpers
# ===========================================================================
def _run(cmd, timeout=10):
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              text=True)
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def system_info(sys_cfg, mock=False):
    out = {}

    if mock:
        # Believable values so the System tab renders in a PC walkthrough.
        out["firewall"] = {"enabled": True,
                           "summary": "rmnet0 inbound dropped (mock)"}
        out["tailscale"] = {"running": True, "state": "up",
                            "ip": "100.100.100.100", "hostname": "uz801-mock"}
        out["adguard"] = {"running": False, "summary": "off"}
        out["audio"] = audiobridge.probe()
        out["services"] = [
            {"name": "tailscale", "status": "running",
             "description": "Tailscale mesh VPN"},
            {"name": "adguard", "status": "stopped",
             "description": "AdGuard Home DNS filtering"},
        ]
        return out

    # Firewall: dump the iptables ruleset (best effort).
    code, stdout, _ = _run(sys_cfg.get("firewall_cmd", ["iptables", "-S"]))
    rules = stdout.strip().splitlines() if code == 0 else []
    has_rules = any(r.startswith("-A") for r in rules)
    out["firewall"] = {
        "enabled": code == 0 and has_rules,
        "summary": ("%d rules" % sum(1 for r in rules if r.startswith("-A")))
        if has_rules else "no rules",
    }

    # Tailscale status (JSON if available).
    code, stdout, _ = _run(sys_cfg.get("tailscale_status_cmd",
                                       ["tailscale", "status", "--json"]))
    if code == 0 and stdout.strip():
        try:
            ts = json.loads(stdout)
            ips = (ts.get("Self") or {}).get("TailscaleIPs") or []
            out["tailscale"] = {
                "running": (ts.get("BackendState") == "Running"),
                "state": ts.get("BackendState", "unknown"),
                "ip": ips[0] if ips else None,
                "hostname": (ts.get("Self") or {}).get("HostName"),
            }
        except ValueError:
            out["tailscale"] = {"running": True, "state": "up", "ip": None,
                                "hostname": None}
    else:
        out["tailscale"] = {"running": False, "state": "down", "ip": None,
                            "hostname": None}

    # AdGuard: is the configured port listening?
    port = (sys_cfg.get("adguard_check") or {}).get("port", 3000)
    ag_up = _port_open("127.0.0.1", port)
    out["adguard"] = {"running": ag_up,
                      "summary": "DNS filtering on" if ag_up else "off"}

    # Audio bridge availability (feature flag probe).
    out["audio"] = audiobridge.probe()

    # Service states as a list of {name, status, description}.
    services = []
    for name, actions in (sys_cfg.get("services") or {}).items():
        status = "unknown"
        if "status" in actions:
            code, stdout, _ = _run(actions["status"])
            status = "running" if code == 0 else "stopped"
        services.append({"name": name, "status": status,
                         "description": actions.get("description", "")})
    out["services"] = services
    return out


def _port_open(host, port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        s.close()


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- WiFi encryption mapping ------------------------------------------------
# This device's stock encryp_type values:
#   0=None, 1=WPA-TKIP, 2=WPA-CCMP, 3=WPA2-TKIP, 4=WPA2-CCMP
# The SPA/contract uses: open | wpa2 | wpa2wpa3 | wpa3
# The hostapd on this KitKat-era firmware has NO WPA3 support, so wpa3 and the
# wpa2/wpa3 mixed mode both degrade to the best available (WPA2-CCMP = 4) with
# a warning surfaced to the client.
def _api_to_encryp(encryption):
    """Return (encryp_type:int, note:str|None) for an API encryption value."""
    enc = (encryption or "").lower()
    if enc == "open":
        return 0, None
    if enc == "wpa2":
        return 4, None
    if enc == "wpa2wpa3":
        return 4, ("WPA2/WPA3 mixed mode is not supported by this device's "
                   "hostapd; saved as WPA2-CCMP.")
    if enc == "wpa3":
        return 4, ("WPA3 is not supported by this device's hostapd; saved as "
                   "the best available WPA2-CCMP.")
    # Unknown -> safest secured default.
    return 4, None


def _encryp_to_api(encryp_type):
    """Map a device encryp_type back to an API encryption value."""
    val = _to_int(encryp_type)
    if val == 0:
        return "open"
    # 1..4 are all WPA/WPA2 variants; the closest API value is wpa2.
    return "wpa2"


def _sms_row(r):
    return {
        "id": str(r["id"]), "box": r["box"], "address": r["address"],
        "text": r["text"], "ts": r["ts"], "status": r["status"],
        "unread": not bool(r["is_read"]), "modem_index": r["modem_index"],
    }


# ===========================================================================
# Bootstrap
# ===========================================================================
def load_config(path):
    cfg = DEFAULT_CONFIG
    if path and os.path.isfile(path):
        with open(path) as fh:
            cfg = deep_merge(DEFAULT_CONFIG, json.load(fh))
    return cfg


def main():
    ap = argparse.ArgumentParser(description="cockpit-api secure dongle API")
    ap.add_argument("--config", default="config.json",
                    help="path to JSON config (default: config.json)")
    ap.add_argument("--mock", action="store_true",
                    help="use a fake modem (no hardware; for PC development)")
    ap.add_argument("--host", default=None, help="override bind host")
    ap.add_argument("--port", type=int, default=None, help="override bind port")
    ap.add_argument("--db", default=None, help="override db_path")
    ap.add_argument("--no-tls", action="store_true",
                    help="serve plain HTTP (DEV ONLY; never on the device)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    mock = args.mock or cfg["modem"].get("mock", False)
    use_tls = cfg["tls"].get("enabled", True) and not args.no_tls

    # Resolve the DB path. In mock/dev the default /data/... path is usually not
    # writable on a PC, so fall back to a local file.
    if args.db:
        cfg["db_path"] = args.db
    elif mock:
        parent = os.path.dirname(cfg["db_path"]) or "."
        if not os.access(parent, os.W_OK):
            cfg["db_path"] = os.path.join(os.getcwd(), "cockpit-mock.db")

    logutil.setup(level=cfg["log"].get("level", "INFO"),
                  file_path=cfg["log"].get("file"))

    app = App(cfg, mock=mock, tls_enabled=use_tls)
    app.start_sms_poller()
    app.start_link_poller()

    server = ThreadingHTTPServer((cfg["host"], cfg["port"]), Handler)
    server.app = app
    server.daemon_threads = True
    if use_tls:
        cert, key = tlsutil.ensure_cert(cfg["tls"])
        ctx = tlsutil.make_context(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"

    # Optional audio bridge on its own port, sharing the login check.
    audio_cfg = cfg.get("audio", {})
    if audio_cfg.get("enabled"):
        def auth_check(head):
            # Validate the session cookie carried in the WS upgrade request.
            for line in head.split("\r\n"):
                if line.lower().startswith("cookie:"):
                    jar = SimpleCookie()
                    try:
                        jar.load(line.split(":", 1)[1].strip())
                    except Exception:  # noqa: BLE001
                        return False
                    morsel = jar.get(COOKIE_NAME)
                    if morsel and app.auth.session_from_cookie(morsel.value):
                        return True
            return False

        ws_ctx = None
        if use_tls:
            ws_ctx = tlsutil.make_context(cfg["tls"]["cert"], cfg["tls"]["key"])
        bridge = audiobridge.AudioBridgeServer(
            host=audio_cfg.get("host", "0.0.0.0"),
            port=audio_cfg.get("port", 8089),
            tls_context=ws_ctx,
            auth_check=auth_check,
            rate=audio_cfg.get("rate", 8000),
        )
        threading.Thread(target=bridge.serve_forever, daemon=True).start()

    log.event(20, "server.listen", scheme=scheme, host=cfg["host"],
              port=cfg["port"], mock=mock,
              first_run=app.auth.first_run())
    print("cockpit-api listening on %s://%s:%d  (mock=%s, first_run=%s)"
          % (scheme, cfg["host"], cfg["port"], mock, app.auth.first_run()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        app.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
