#!/usr/bin/env python3
"""
Local reverse proxy + static file server for the UZ801 4G dongle web UI.

Why this exists
---------------
The dongle's management API lives at  http://192.168.100.1/ajax  (plain HTTP).
A browser page served from any other origin cannot talk to it directly:
  * CORS      - the device does not send Access-Control-Allow-Origin headers.
  * mixed-content - an https page may not POST to a plain-http endpoint.

So we run a tiny proxy *on the user's machine* that:
  (a) serves the static UI files in this directory, and
  (b) forwards  POST /ajax  (JSON *and* multipart firmware uploads) to the
      device, returning the device's response verbatim.

Both the UI and the /ajax calls then share one origin (http://localhost:8088),
so the browser is happy.

Usage
-----
    python3 proxy.py
    # then open  http://localhost:8088/

Configuration (environment variables, all optional)
---------------------------------------------------
    DONGLE_URL   base URL of the device      (default http://192.168.100.1)
    PORT         local port to listen on     (default 8088)
    HOST         local bind address          (default 127.0.0.1)

Only the Python 3 standard library is used - no pip install required.
"""

import os
import sys
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError

# --- Configuration -----------------------------------------------------------
# Base URL of the dongle. Override with:  DONGLE_URL=http://10.0.0.1 python3 proxy.py
DEVICE_BASE = os.environ.get("DONGLE_URL", "http://192.168.100.1").rstrip("/")
PORT = int(os.environ.get("PORT", "8088"))
HOST = os.environ.get("HOST", "127.0.0.1")

# Directory that holds the static UI (this file's directory).
WEBROOT = os.path.dirname(os.path.abspath(__file__))

# How long to wait on the device before giving up (seconds).
DEVICE_TIMEOUT = 20


class Handler(BaseHTTPRequestHandler):
    # Quieter, single-line logging.
    def log_message(self, fmt, *args):
        sys.stderr.write("[proxy] %s - %s\n" % (self.address_string(), fmt % args))

    # ---- static file serving (GET) -----------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/":
            path = "/index.html"

        # Resolve against WEBROOT and refuse any path traversal.
        safe = os.path.normpath(os.path.join(WEBROOT, path.lstrip("/")))
        if not safe.startswith(WEBROOT):
            self.send_error(403, "Forbidden")
            return
        if not os.path.isfile(safe):
            self.send_error(404, "Not found: %s" % path)
            return

        ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        try:
            with open(safe, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            self.send_error(500, "Read error: %s" % exc)
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- API proxy (POST /ajax) ---------------------------------------------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/ajax":
            self.send_error(404, "Only /ajax is proxied")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""

        # Preserve the incoming Content-Type so multipart boundaries survive.
        ctype = self.headers.get("Content-Type", "application/json")

        target = DEVICE_BASE + "/ajax"
        req = urlrequest.Request(target, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        # The device is picky about very little, but send a plain UA.
        req.add_header("User-Agent", "uz801-webui-proxy")
        # Forward the session cookie so login state (funcNo 1000) carries over.
        cookie = self.headers.get("Cookie")
        if cookie:
            req.add_header("Cookie", cookie)

        try:
            with urlrequest.urlopen(req, timeout=DEVICE_TIMEOUT) as resp:
                data = resp.read()
                resp_ctype = resp.headers.get("Content-Type", "application/json")
                # Pass any Set-Cookie headers back so the browser keeps the session.
                extra = [("Set-Cookie", v)
                         for v in resp.headers.get_all("Set-Cookie") or []]
                self._send_bytes(resp.status, data, resp_ctype, extra)
        except HTTPError as exc:
            # Device answered with an HTTP error - pass its body through.
            data = exc.read() if hasattr(exc, "read") else b""
            self._send_bytes(exc.code, data or self._err(str(exc)),
                             exc.headers.get("Content-Type", "application/json"))
        except URLError as exc:
            # Device unreachable (offline, wrong IP, not connected). Report as a
            # JSON error the UI understands, with a 502 so fetch() can flag it.
            msg = "device unreachable at %s (%s)" % (DEVICE_BASE, exc.reason)
            self._send_bytes(502, self._err(msg), "application/json")
        except Exception as exc:  # pragma: no cover - defensive
            self._send_bytes(502, self._err("proxy error: %s" % exc),
                             "application/json")

    # ---- helpers ------------------------------------------------------------
    def _send_bytes(self, status, data, ctype, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    @staticmethod
    def _err(message):
        return json.dumps(
            {"results": [], "error_info": message, "flag": "0"}
        ).encode("utf-8")


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("UZ801 web UI proxy")
    print("  serving UI   : http://%s:%d/" % (HOST, PORT))
    print("  forwarding   : POST /ajax  ->  %s/ajax" % DEVICE_BASE)
    print("  (change the device with  DONGLE_URL=http://IP python3 proxy.py)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
