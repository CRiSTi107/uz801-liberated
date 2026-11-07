"""
stockproxy.py - allow-listed passthrough to the stock MiFi web API.

The dongle's stock, UNAUTHENTICATED JSON API lives at ``http://127.0.0.1:80/ajax``
and takes ``{"funcNo": N, ...}``. cockpit-api sits in front of it and only
forwards funcNos that are on an explicit allow-list, and only for already-
authenticated users. The dangerous ones are blocked outright; IMEI-write
(funcNo 2002) is allowed solely when an explicit danger flag is supplied by the
caller.

NOTE on funcNo 2002: on this firmware (UZ801 V2.3.12) the stock UI JS does not
actually expose a 2002 "write IMEI" function - IMEI writes on this board go via
``AT+WRIMEI`` (see modem.write_imei / POST /api/sim/imei). 2002 is kept in the
gated set exactly as the contract specifies, so that if a variant firmware does
expose it, it still requires the danger flag. The modem AT path remains the
real IMEI-write mechanism.

Only the Python standard library is used.
"""

import json
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError

from logutil import get

log = get("cockpit.stock")

# Read / WiFi / LAN functions safe for any logged-in user (per the contract).
ALLOW_READ = {
    1001, 1002, 1003, 1005, 1006, 1007, 1009, 1010, 1011,
    1015, 1016, 1017, 1018, 1029,
    1052, 1053, 1054, 1055,
    3002,
}

# Explicitly dangerous - never forwarded by the normal path.
BLOCK = {
    2000,   # EDL / reboot to emergency download
    2001,   # enable ADB / USB debug (the stock backdoor)
    3000,   # firmware flash / apply upgrade
}

# Allowed ONLY when the caller passes an explicit danger flag.
DANGER_GATED = {
    2002,   # IMEI write (see module docstring)
}


class StockProxyError(Exception):
    def __init__(self, message, status=403):
        super().__init__(message)
        self.status = status


class StockProxy:
    def __init__(self, base_url="http://127.0.0.1:80", timeout=15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_allowed(self, func_no, danger=False):
        if func_no in BLOCK:
            return False
        if func_no in DANGER_GATED:
            return bool(danger)
        return func_no in ALLOW_READ

    def forward(self, payload, danger=False):
        """Forward an allow-listed funcNo call. ``payload`` is the full dict.

        Raises StockProxyError (403) if the funcNo is not permitted.
        """
        try:
            func_no = int(payload.get("funcNo"))
        except (TypeError, ValueError):
            raise StockProxyError("missing or invalid funcNo", status=400)

        if not self.is_allowed(func_no, danger=danger):
            reason = "blocked"
            if func_no in DANGER_GATED:
                reason = "requires danger flag"
            log.event(30, "stock.denied", funcNo=func_no, reason=reason)
            raise StockProxyError(
                "funcNo %d is not permitted (%s)" % (func_no, reason), status=403)

        body = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(self.base_url + "/ajax", data=body,
                                 method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "cockpit-api-stockproxy")
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except HTTPError as exc:
            raw = exc.read() if hasattr(exc, "read") else b""
            log.event(30, "stock.http_error", funcNo=func_no, code=exc.code)
        except URLError as exc:
            log.event(40, "stock.unreachable", funcNo=func_no, err=str(exc.reason))
            raise StockProxyError(
                "stock API unreachable: %s" % exc.reason, status=502)

        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError):
            # Return the raw text wrapped, so the frontend still sees something.
            return {"raw": raw.decode("utf-8", "replace")}


class MockStockProxy(StockProxy):
    """Offline stand-in for the stock API used in ``--mock`` mode.

    Honours the same allow-list / block / danger gating as the real proxy
    (so security behaviour is identical in tests) but returns canned replies
    for the funcNos the service calls instead of hitting 127.0.0.1:80.
    """

    def __init__(self, base_url="http://127.0.0.1:80", timeout=15):
        super().__init__(base_url, timeout)
        # Mutable WiFi state so set/get round-trips in a mock walkthrough.
        self._wifi = {
            "ssid": "UZ801-mock",
            "pwd": "mockpass123",
            "encryp_type": 4,   # WPA2-CCMP
            "maxSta": 8,
        }

    def forward(self, payload, danger=False):
        try:
            func_no = int(payload.get("funcNo"))
        except (TypeError, ValueError):
            raise StockProxyError("missing or invalid funcNo", status=400)
        if not self.is_allowed(func_no, danger=danger):
            reason = "requires danger flag" if func_no in DANGER_GATED \
                else "blocked"
            raise StockProxyError(
                "funcNo %d is not permitted (%s)" % (func_no, reason),
                status=403)
        return self._canned(func_no, payload)

    def _canned(self, func_no, payload):
        if func_no == 1003:   # traffic counters
            return {"results": [{"up": 1048576, "down": 20971520}]}
        if func_no == 1005:   # net mode
            return {"net_mode": payload.get("net_mode", "auto")}
        if func_no == 1006:   # wifi read
            return dict(self._wifi)
        if func_no == 1007:   # wifi set
            for key in ("ssid", "pwd", "encryp_type", "maxSta"):
                if key in payload:
                    self._wifi[key] = payload[key]
            return {"result": "ok"}
        if func_no == 1010:   # wifi clients
            return {"clients": [
                {"hostname": "phone", "ip": "192.168.100.101",
                 "mac": "aa:bb:cc:dd:ee:01"},
                {"hostname": "laptop", "ip": "192.168.100.102",
                 "mac": "aa:bb:cc:dd:ee:02"},
            ]}
        if func_no == 1017:   # apn profile set
            return {"result": "ok"}
        return {"result": "ok", "funcNo": func_no}
