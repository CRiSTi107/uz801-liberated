"""
modem.py - thread-safe AT-command access to the UZ801 modem via /dev/smd11,
plus higher-level status / SIM / SMS / call / network helpers.

Low level
---------
The on-device ``modem_at`` tool simply writes ``"<AT>\\r"`` to ``/dev/smd11``
(a 0666 shared-memory-driver char node) and reads the modem's reply until a
final-result line (``OK`` / ``ERROR`` / ``+CME ERROR`` / ``+CMS ERROR``) or a
timeout. We emulate exactly that here. Because many callers (HTTP request
threads, the SMS poller) share the single modem channel, every transaction is
serialised with a lock so only one AT exchange is on the wire at a time.

The device path is configurable (default ``/dev/smd11``) so it can be pointed
at a FIFO/file for offline development, and a ``--mock`` mode swaps in a fake
modem so the whole service runs with no hardware.

Only the Python standard library is used.
"""

import os
import re
import select
import threading
import time

from logutil import get
import smspdu

log = get("cockpit.modem")

# Final-result codes that terminate a transaction.
_FINAL_OK = "OK"
_FINAL_ERRORS = ("ERROR",)
_CME_RE = re.compile(r"^\+(CME|CMS) ERROR:", re.I)

# EARFCN -> (band label, approximate downlink MHz). Values that the task spec
# calls out explicitly; extend as more cells are observed on this device.
_EARFCN_BANDS = {
    # Cells actually observed on this device (Digi RO).
    525: ("B1", 2100),
    3350: ("B7", 2600),   # seen on the balcony - fades hard, not usable here
    6425: ("B20", 800),   # strongest cell indoors - hardware can't use 800 MHz
}
# Range fallbacks (3GPP downlink E-UTRA EARFCN ranges). Covers the bands this
# board supports (B1/B3/B5/B8/B40) plus the ones it keeps camping on but can't
# use (B7/B20), so both get labelled rather than showing as unknown.
_EARFCN_RANGES = [
    (0, 599, "B1", 2100),
    (1200, 1949, "B3", 1800),
    (2400, 2649, "B5", 850),
    (2750, 3449, "B7", 2600),
    (3450, 3799, "B8", 900),
    (6150, 6449, "B20", 800),
    (38650, 39649, "B40", 2300),
]


def earfcn_to_band(earfcn):
    """Map a downlink EARFCN to a ``(band_label, freq_mhz)`` tuple."""
    try:
        e = int(earfcn)
    except (TypeError, ValueError):
        return (None, None)
    if e in _EARFCN_BANDS:
        return _EARFCN_BANDS[e]
    for lo, hi, band, mhz in _EARFCN_RANGES:
        if lo <= e <= hi:
            return (band, mhz)
    return (None, None)


# LTE E-UTRA bands this UZ801 hardware supports (FDD B1/B3/B5/B8 + TDD B40).
# In the Qualcomm LTE band bitmask, E-UTRA band N occupies bit (N-1).
LTE_BANDS_SUPPORTED = ["B1", "B3", "B5", "B8", "B40"]


def _band_num(label):
    """'B3' / 'b3' / '3' -> 3 ; anything else -> None."""
    m = re.match(r"[Bb]?(\d+)$", str(label).strip())
    return int(m.group(1)) if m else None


def lte_band_mask(bands):
    """Build the Qualcomm LTE band bitmask (E-UTRA band N -> bit N-1).

    ``bands``: 'auto'/None/empty -> all supported bands; a single label
    ('B3'); or a list (['B1','B3']). Returns an int bitmask.
    """
    if not bands or (isinstance(bands, str) and bands.strip().lower() == "auto"):
        bands = LTE_BANDS_SUPPORTED
    if isinstance(bands, str):
        bands = [bands]
    mask = 0
    for b in bands:
        n = _band_num(b)
        if n:
            mask |= 1 << (n - 1)
    return mask


# Bands that this UZ801 hardware (with its tiny internal antenna) can camp on
# but cannot sustain a usable link on. B20 (800 MHz) is the well-known antenna
# mismatch on this board; B7 (2600 MHz) is similarly unusable here.
_UNUSABLE_BANDS = {"B20", "B7"}
# Bands known to work acceptably on this hardware.
_USABLE_BANDS = {"B1", "B3", "B5", "B8", "B40"}


def band_usable(band):
    """Return ``(usable: bool, hint: str|None)`` for a band label.

    ``band is None`` (no reading yet) is treated as usable so the UI does not
    nag before a cell is acquired. Known-bad bands return a specific hint;
    unrecognised bands are flagged as possibly unsupported.
    """
    if not band:
        return (True, None)
    if band in _UNUSABLE_BANDS:
        return (False, "Band %s is not usable on this hardware's antenna - "
                       "signal will be weak and the link may drop. Lock to a "
                       "usable band (e.g. B3/B1/B8) or improve the antenna."
                       % band)
    if band not in _USABLE_BANDS:
        return (False, "Band %s may be unsupported on this hardware." % band)
    return (True, None)


class AtResult:
    """Parsed outcome of a single AT transaction."""

    def __init__(self, command, lines, status, timed_out=False):
        self.command = command
        self.lines = lines              # intermediate response lines (no echo)
        self.status = status            # 'OK' | 'ERROR' | '+CME ERROR: ...' | 'TIMEOUT'
        self.timed_out = timed_out

    @property
    def ok(self):
        return self.status == _FINAL_OK

    @property
    def raw(self):
        body = "\n".join(self.lines)
        return (body + ("\n" if body else "") + self.status).strip()

    def __repr__(self):
        return "AtResult(cmd=%r, status=%r, lines=%r)" % (
            self.command, self.status, self.lines)


# ===========================================================================
# Low-level transport
# ===========================================================================
class Modem:
    """Serialised AT transport over a character device (default /dev/smd11)."""

    def __init__(self, device="/dev/smd11", timeout=8.0):
        self.device = device
        self.timeout = timeout
        self._lock = threading.RLock()
        self._fd = None

    def _ensure_open(self):
        if self._fd is None:
            # O_RDWR: the smd node is bidirectional. O_NONBLOCK so a dead
            # channel can't wedge the open; reads use select() for timing.
            self._fd = os.open(self.device, os.O_RDWR | os.O_NONBLOCK)
            log.event(10, "modem.open", device=self.device)

    def close(self):
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None

    def _drain(self):
        """Discard any stale bytes sitting in the channel before a new command."""
        try:
            while True:
                r, _, _ = select.select([self._fd], [], [], 0)
                if not r:
                    return
                if not os.read(self._fd, 4096):
                    return
        except (OSError, BlockingIOError):
            return

    def transaction(self, command, timeout=None):
        """Run one AT command and return an AtResult. Thread-safe.

        ``command`` may include or omit the ``AT`` prefix and must NOT include
        the trailing CR (we add ``\\r``).
        """
        timeout = self.timeout if timeout is None else timeout
        cmd = command.strip()
        with self._lock:
            try:
                self._ensure_open()
                self._drain()
                os.write(self._fd, (cmd + "\r").encode("utf-8", "replace"))
            except OSError as exc:
                log.event(40, "modem.write_error", cmd=cmd, err=str(exc))
                self.close()
                return AtResult(cmd, [], "ERROR: %s" % exc)
            return self._read_until_final(cmd, timeout)

    def _read_until_final(self, cmd, timeout):
        deadline = time.monotonic() + timeout
        buffer = b""
        lines = []
        status = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return AtResult(cmd, lines, "TIMEOUT", timed_out=True)
            r, _, _ = select.select([self._fd], [], [], remaining)
            if not r:
                return AtResult(cmd, lines, "TIMEOUT", timed_out=True)
            try:
                chunk = os.read(self._fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                self.close()
                return AtResult(cmd, lines, "ERROR: %s" % exc)
            if not chunk:
                continue
            buffer += chunk
            # Normalise line endings and split out complete lines.
            buffer = buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                # Drop the command echo some modems emit.
                if line.upper() == cmd.upper():
                    continue
                if line == _FINAL_OK:
                    status = _FINAL_OK
                elif line in _FINAL_ERRORS:
                    status = line
                elif _CME_RE.match(line):
                    status = line
                else:
                    lines.append(line)
                    continue
                return AtResult(cmd, lines, status)


# ===========================================================================
# Mock transport - canned replies, no hardware required
# ===========================================================================
class MockModem:
    """A fake modem for offline development (``--mock``).

    Returns believable responses for the commands the service issues. Keeps a
    tiny in-memory SMS store so send/list/delete behave end-to-end.
    """

    def __init__(self, device="(mock)", timeout=8.0):
        self.device = device
        self.timeout = timeout
        self._lock = threading.RLock()
        self._next_cmgl_index = 1
        self._messages = [
            # (index, stat, deliver-pdu) - one unread inbox message.
            (1, 0, self._sample_pdu()),
        ]
        self._sms_text_mode = True

    @staticmethod
    def _sample_pdu():
        # A canonical SMS-DELIVER PDU: from +31641600986, GSM-7 "hellohello".
        # (Using a known-good DELIVER constant rather than a SUBMIT PDU so the
        # decoder exercises the real inbound path in mock mode.)
        return ("07911326040000F0040B911346610089F6000020806291731408"
                "0AC8329BFD0695E5E937")

    def close(self):
        pass

    def transaction(self, command, timeout=None):
        cmd = command.strip()
        up = cmd.upper()
        with self._lock:
            lines = self._respond(up, cmd)
        if lines is None:
            return AtResult(cmd, [], "ERROR")
        return AtResult(cmd, lines, _FINAL_OK)

    def _respond(self, up, cmd):
        if up in ("AT", "ATZ", "ATE0"):
            return []
        if up.startswith("AT+CMGF"):
            self._sms_text_mode = up.endswith("=1")
            return []
        if up == "AT$QCRSRP?":
            return ["$QCRSRP: 260,525,-105.3,310,6425,-78.1"]
        if up == "AT$QCRSRQ?":
            return ["$QCRSRQ: 260,525,-11.0"]
        if up == "AT+CSQ":
            return ["+CSQ: 18,99"]
        if up == "AT+CEREG?":
            return ["+CEREG: 2,1,\"1A2B\",\"01234567\",7"]
        if up == "AT+COPS?":
            return ["+COPS: 0,0,\"Digi mock\",7"]
        if up == "AT+CGATT?":
            return ["+CGATT: 1"]
        if up.startswith("AT+CGPADDR"):
            return ["+CGPADDR: 1,\"10.74.88.195\""]
        if up == "AT+CGDCONT?":
            return ["+CGDCONT: 1,\"IP\",\"internet\",\"10.74.88.195\",0,0"]
        if up == "AT+CGSN":
            return ["8600180460XXXXX"]
        if up == "AT+CIMI":
            return ["22605XXXXXXXXXX"]
        if up == "AT+CPIN?":
            return ["+CPIN: READY"]
        if up in ("AT+CCID", "AT+ICCID", "AT^ICCID", "AT+QCCID"):
            return ["+CCID: 8940011612345678912"]
        if up == "AT+CNUM":
            return ['+CNUM: "","+40712345678",145']
        if up.startswith("AT+WRIMEI="):
            return []
        if up.startswith("AT+CMGL"):
            out = []
            for index, stat, pdu in self._messages:
                tpdu_len = len(bytes.fromhex(pdu)) - 1
                out.append("+CMGL: %d,%d,,%d" % (index, stat, tpdu_len))
                out.append(pdu)
            return out
        if up.startswith("AT+CMGR="):
            idx = int(re.findall(r"=(\d+)", cmd)[0])
            for index, stat, pdu in self._messages:
                if index == idx:
                    tpdu_len = len(bytes.fromhex(pdu)) - 1
                    return ["+CMGR: %d,,%d" % (stat, tpdu_len), pdu]
            return []
        if up.startswith("AT+CMGS="):
            # In PDU mode the modem returns a prompt; our send() handles the
            # two-step exchange. Mock just acks with a reference.
            return ["+CMGS: 42"]
        if up.startswith("AT+CMGD="):
            idx = int(re.findall(r"=(\d+)", cmd)[0])
            self._messages = [m for m in self._messages if m[0] != idx]
            return []
        if up.startswith("AT+CNMI"):
            return []
        if up.startswith("ATD") or up.startswith("AT+D"):
            return []
        if up == "ATA" or up == "ATH":
            return []
        if up.startswith("AT+CLCC"):
            return []  # no active call
        if up.startswith("AT+CGDCONT="):
            return []
        # Default: pretend success for unknown writes, empty for queries.
        return []


# ===========================================================================
# High-level control surface
# ===========================================================================
class ModemControl:
    """Higher-level modem operations returning JSON-friendly dicts.

    Wraps a transport (``Modem`` or ``MockModem``) and the SMS send prompt
    protocol. Every method is safe to call from multiple threads because the
    underlying transport serialises transactions.
    """

    def __init__(self, transport):
        self.t = transport

    # --- raw passthrough -----------------------------------------------------
    def at(self, command, timeout=None):
        return self.t.transaction(command, timeout=timeout)

    # --- signal / status -----------------------------------------------------
    def signal(self):
        """Return rsrp, rsrq, sinr, band, earfcn, pci, csq, bars, cells."""
        out = {"rsrp": None, "rsrq": None, "sinr": None, "band": None,
               "earfcn": None, "pci": None, "csq": None, "bars": 0,
               "cells": []}

        res = self.at("AT$QCRSRP?")
        best = None
        for line in res.lines:
            # $QCRSRP: <plmn>,<pci>,<rsrp>,<pci2>,<earfcn>... formats vary; we
            # parse triplets of (pci, earfcn, rsrp) defensively from the numbers.
            nums = re.findall(r"-?\d+\.?\d*", line.split(":", 1)[-1])
            # Observed form on this device: pci, earfcn, rsrp[, pci, earfcn, rsrp...]
            for i in range(0, len(nums) - 2, 3):
                try:
                    pci = int(float(nums[i]))
                    earfcn = int(float(nums[i + 1]))
                    rsrp = float(nums[i + 2])
                except ValueError:
                    continue
                band, mhz = earfcn_to_band(earfcn)
                cell = {"pci": pci, "earfcn": earfcn, "rsrp": rsrp,
                        "band": band, "freq_mhz": mhz}
                out["cells"].append(cell)
                # Serving cell = strongest RSRP.
                if best is None or rsrp > best["rsrp"]:
                    best = cell
        if best:
            out["rsrp"] = best["rsrp"]
            out["earfcn"] = best["earfcn"]
            out["band"] = best["band"]
            out["pci"] = best["pci"]

        res = self.at("AT$QCRSRQ?")
        for line in res.lines:
            nums = re.findall(r"-?\d+\.?\d*", line.split(":", 1)[-1])
            if len(nums) >= 3:
                try:
                    out["rsrq"] = float(nums[2])
                except ValueError:
                    pass

        res = self.at("AT+CSQ")
        for line in res.lines:
            m = re.search(r"\+CSQ:\s*(\d+),(\d+)", line)
            if m:
                rssi = int(m.group(1))
                out["csq"] = rssi
                out["bars"] = _csq_to_bars(rssi)
        return out

    def registration(self):
        """Return {state, registered, operator, rat, cell_id, tac}."""
        state_map = {0: "not registered", 1: "registered", 2: "searching",
                     3: "denied", 4: "unknown", 5: "roaming"}
        registered_codes = {1, 5}  # 1=home, 5=roaming
        rat_map = {0: "GSM", 2: "UTRAN", 3: "GSM/EGPRS", 4: "HSDPA",
                   5: "HSUPA", 6: "HSPA", 7: "LTE"}
        reg = {"state": None, "registered": False, "operator": None,
               "rat": None, "cell_id": None, "tac": None}

        res = self.at("AT+CEREG?")
        for line in res.lines:
            # +CEREG: <n>,<stat>[,"<tac>","<ci>",<AcT>]
            m = re.search(
                r'\+CEREG:\s*\d+,(\d+)'
                r'(?:,"?([0-9A-Fa-f]*)"?,"?([0-9A-Fa-f]+)"?,(\d+))?', line)
            if m:
                code = int(m.group(1))
                reg["state"] = state_map.get(code, m.group(1))
                reg["registered"] = code in registered_codes
                if m.group(2):
                    reg["tac"] = "0x" + m.group(2)
                if m.group(3):
                    reg["cell_id"] = "0x" + m.group(3).upper()
                if m.group(4) is not None:
                    reg["rat"] = rat_map.get(int(m.group(4)), m.group(4))

        res = self.at("AT+COPS?")
        for line in res.lines:
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]*)"(?:,(\d+))?', line)
            if m:
                reg["operator"] = m.group(1)
                if m.group(2) is not None and reg["rat"] is None:
                    reg["rat"] = rat_map.get(int(m.group(2)), m.group(2))
        return reg

    def data_status(self):
        """Return {connected, ip, up_bytes, down_bytes}."""
        data = {"connected": False, "ip": None, "up_bytes": None,
                "down_bytes": None}
        res = self.at("AT+CGATT?")
        for line in res.lines:
            m = re.search(r"\+CGATT:\s*(\d+)", line)
            if m:
                data["connected"] = m.group(1) == "1"
        res = self.at("AT+CGPADDR")
        for line in res.lines:
            m = re.search(r'\+CGPADDR:\s*\d+,"?([0-9a-fA-F:.]+)"?', line)
            if m and m.group(1) not in ("0.0.0.0", ""):
                data["ip"] = m.group(1)
        # up/down byte counters are not available over AT on this modem; the
        # server fills them from funcNo 1003 (stock traffic) where possible.
        return data

    def contexts(self):
        """Parse AT+CGDCONT? into a list of PDP context dicts."""
        res = self.at("AT+CGDCONT?")
        ctxs = []
        for line in res.lines:
            m = re.search(r'\+CGDCONT:\s*(\d+),"([^"]*)","([^"]*)"', line)
            if m:
                ctxs.append({"cid": int(m.group(1)), "pdp_type": m.group(2),
                             "apn": m.group(3)})
        return ctxs

    # --- SIM / identity ------------------------------------------------------
    def imei(self):
        res = self.at("AT+CGSN")
        for line in res.lines:
            m = re.search(r"\d{14,15}", line)
            if m:
                return m.group(0)
        return None

    def imsi(self):
        res = self.at("AT+CIMI")
        for line in res.lines:
            m = re.search(r"\d{10,15}", line)
            if m:
                return m.group(0)
        return None

    def cpin(self):
        res = self.at("AT+CPIN?")
        for line in res.lines:
            m = re.search(r"\+CPIN:\s*(.+)", line)
            if m:
                return m.group(1).strip()
        return "ERROR" if not res.ok else None

    def iccid(self):
        """SIM ICCID via AT+CCID (fallbacks for Qualcomm/other firmwares)."""
        for cmd in ("AT+CCID", "AT+ICCID", "AT^ICCID", "AT+QCCID"):
            res = self.at(cmd)
            if not res.ok:
                continue
            for line in res.lines:
                m = re.search(r"([0-9][0-9A-Fa-f]{17,21})", line)
                if m:
                    return m.group(1)
        return None

    def msisdn(self):
        """Subscriber number via AT+CNUM (often empty on prepaid SIMs)."""
        res = self.at("AT+CNUM")
        for line in res.lines:
            m = re.search(r'\+CNUM:\s*(?:"[^"]*")?,\s*"?(\+?\d+)"?', line)
            if m:
                return m.group(1)
        return None

    def write_imei(self, imei):
        """DANGEROUS: write a new IMEI via AT+WRIMEI. Caller must gate this."""
        if not re.fullmatch(r"\d{15}", imei or ""):
            raise ValueError("IMEI must be exactly 15 digits")
        if not _luhn_ok(imei):
            raise ValueError("IMEI fails Luhn checksum")
        res = self.at("AT+WRIMEI=%s" % imei, timeout=15)
        return res

    # --- SMS -----------------------------------------------------------------
    def sms_set_text_mode(self, text=True):
        return self.at("AT+CMGF=%d" % (1 if text else 0))

    def sms_set_cnmi(self):
        # Route new-message indications as +CMTI (store + notify), no direct
        # delivery to the TE, so our poller controls reads.
        return self.at("AT+CNMI=2,1,0,0,0")

    def sms_list_pdu(self, stat=4):
        """List messages in PDU mode. stat 4 = ALL. Returns list of dicts."""
        self.at("AT+CMGF=0")
        res = self.at("AT+CMGL=%d" % stat, timeout=15)
        return _parse_cmgl_pdu(res.lines)

    def sms_read_pdu(self, index):
        self.at("AT+CMGF=0")
        res = self.at("AT+CMGR=%d" % index, timeout=10)
        msgs = _parse_cmgr_pdu(res.lines, index)
        return msgs[0] if msgs else None

    def sms_delete(self, index):
        return self.at("AT+CMGD=%d" % index)

    def sms_send(self, number, text, require_registration=True):
        """Send an SMS in PDU mode, splitting long/unicode bodies as needed.

        Returns a dict {ok, references:[...], parts:n, error:None|str,
        registered:bool}.
        The CMGS prompt protocol: send ``AT+CMGS=<len>``, wait for ``>``, send
        the PDU hex followed by Ctrl-Z (0x1A).

        Fast-fail: an SMS can only leave the device once the modem is
        registered on a network. On this board that is signal/band-gated (the
        local strong cell is B20, which the antenna can't use), so an
        unregistered send would otherwise crawl through the whole CMGS prompt
        protocol and only then return a cryptic ``+CMS ERROR: 500`` tens of
        seconds later. When ``require_registration`` is set we check
        registration first and reject immediately with a human-readable reason.
        """
        if require_registration:
            reg = self.registration()
            if not reg.get("registered"):
                state = reg.get("state") or "not registered"
                return {"ok": False, "references": [], "parts": 0,
                        "registered": False,
                        "error": "modem not registered (%s) - cannot send "
                                 "SMS; the network link is signal/band-gated "
                                 "on this hardware" % state}
        self.at("AT+CMGF=0")
        import secrets as _secrets
        ref_base = _secrets.randbelow(256)
        pdus = smspdu.encode_submit(number, text, reference=ref_base)
        references = []
        for tpdu_len, pdu in pdus:
            # Two-step: the prompt command, then payload+Ctrl-Z. For transports
            # that expose a raw channel we send both; the mock returns +CMGS.
            res = self._send_pdu(tpdu_len, pdu)
            if not res.ok:
                return {"ok": False, "references": references,
                        "parts": len(pdus), "registered": True,
                        "error": res.status}
            for line in res.lines:
                m = re.search(r"\+CMGS:\s*(\d+)", line)
                if m:
                    references.append(int(m.group(1)))
        return {"ok": True, "references": references, "parts": len(pdus),
                "registered": True, "error": None}

    def _send_pdu(self, tpdu_len, pdu):
        """Perform the AT+CMGS prompt exchange for one PDU."""
        transport = self.t
        # MockModem: single-shot.
        if isinstance(transport, MockModem):
            return transport.transaction("AT+CMGS=%d" % tpdu_len)
        # Real modem: write the prompt command, then the payload + Ctrl-Z in a
        # single locked exchange so nothing interleaves.
        with transport._lock:  # noqa: SLF001 - intentional single-channel lock
            transport._ensure_open()
            transport._drain()
            os.write(transport._fd, ("AT+CMGS=%d\r" % tpdu_len).encode())
            # Wait briefly for the '>' prompt. If it never arrives (e.g. the
            # modem rejected the CMGS outright), do NOT write the payload and
            # block for the full 30s window: abort the pending command with an
            # ESC (0x1B) and fail fast.
            if not _wait_for_prompt(transport._fd, timeout=5):
                try:
                    os.write(transport._fd, b"\x1b")
                except OSError:
                    pass
                return AtResult("AT+CMGS", [], "TIMEOUT: no '>' prompt",
                                timed_out=True)
            os.write(transport._fd, (pdu + "\x1a").encode())
            return transport._read_until_final("AT+CMGS", timeout=30)

    # --- calls ---------------------------------------------------------------
    def dial(self, number):
        # Spec: dial via AT+D<number>; (equivalently ATD<number>;). We use ATD
        # which is universally accepted; ';' requests a voice call.
        safe = re.sub(r"[^0-9+*#A-Dp]", "", number or "")
        if not safe:
            raise ValueError("empty/invalid dial number")
        return self.at("ATD%s;" % safe)

    def answer(self):
        return self.at("ATA")

    def hangup(self):
        return self.at("ATH")

    def call_status(self):
        """Parse AT+CLCC into a list of active calls."""
        res = self.at("AT+CLCC")
        calls = []
        stat_map = {0: "active", 1: "held", 2: "dialing", 3: "alerting",
                    4: "incoming", 5: "waiting"}
        dir_map = {0: "outgoing", 1: "incoming"}
        for line in res.lines:
            m = re.search(r'\+CLCC:\s*(\d+),(\d+),(\d+),(\d+),(\d+)'
                          r'(?:,"([^"]*)")?', line)
            if m:
                calls.append({
                    "id": str(int(m.group(1))),
                    "direction": dir_map.get(int(m.group(2)), "outgoing"),
                    "state": stat_map.get(int(m.group(3)), m.group(3)),
                    "number": m.group(6),
                    # CLCC carries no call duration; the SPA seeds its timer
                    # from this (0 = start counting from now).
                    "duration": 0,
                })
        return calls

    # --- network / APN -------------------------------------------------------
    def set_apn(self, apn, cid=1, pdp_type="IP"):
        return self.at('AT+CGDCONT=%d,"%s","%s"' % (cid, pdp_type, apn))

    def activate_pdp(self, cid=1):
        """Bring up the PDP context (dial data) via AT+CGACT=1,<cid>."""
        return self.at("AT+CGACT=1,%d" % cid, timeout=30)

    # --- RAT / net mode ------------------------------------------------------
    # ``net mode`` on the stock app (funcNo 1005) selects the preferred radio
    # access technology (auto / 4G-only / 3G / 2G). We read the *current* RAT
    # directly from the modem (COPS/CEREG) and, for setting it, issue an AT
    # command. The exact mode-select command is modem-specific; a per-mode
    # template map can be supplied (from config) so the orchestrator can drop
    # in the command verified for this board. With no template we report the
    # set as unsupported-direct and the caller falls back to the stock proxy.
    _RAT_TO_MODE = {
        "LTE": "4g", "HSPA": "3g", "HSDPA": "3g", "HSUPA": "3g",
        "UTRAN": "3g", "GSM/EGPRS": "2g", "GSM": "2g",
    }

    def net_mode(self):
        """Return the current net mode derived directly from the modem.

        {mode: '4g'|'3g'|'2g'|None, rat: '<RAT>'|None, source: 'at'}
        """
        reg = self.registration()
        rat = reg.get("rat")
        return {"mode": self._RAT_TO_MODE.get(rat), "rat": rat,
                "source": "at"}

    def set_net_mode(self, mode, templates=None):
        """Set the preferred RAT via a configured AT template.

        ``templates`` maps a mode string ('auto','4g','3g','2g', or the raw
        funcNo 1005 numeric '0'/'2'/...) to an AT command. Returns an AtResult
        on success, or None when no template is configured for the mode (the
        caller then falls back to the stock proxy).
        """
        if not templates:
            return None
        key = str(mode)
        cmd = templates.get(key)
        if cmd is None:
            # Allow a numeric<->label fallback (e.g. '0' == 'auto').
            alias = {"0": "auto", "2": "4g", "auto": "0", "4g": "2"}.get(key)
            if alias is not None:
                cmd = templates.get(alias)
        if not cmd:
            return None
        return self.at(cmd, timeout=15)

    # --- band lock -----------------------------------------------------------
    # Locking the LTE radio to specific band(s) is how you stop this dongle
    # from wasting time camping on the strong-but-unusable B20 (800 MHz) cell
    # and force it onto a band its antenna can actually use (B1/B3/B8). The
    # exact AT command is Qualcomm-modem-specific and UNVERIFIED on this board,
    # so it is fully config-driven (``band_at`` templates) - mirroring how
    # ``set_net_mode`` handles the same modem-specific-command problem. Nothing
    # is written to the modem unless a template is configured.
    #
    # ``templates`` (from config ``platform.band_at``) may contain:
    #   "set":   a template with a ``{mask}`` (decimal) or ``{mask_hex}`` (no
    #            0x prefix) or ``{mask_hex0x}`` placeholder, e.g.
    #            "AT$QCBANDPREF=0x{mask_hex}" - used for any band selection.
    #   "<BAND>": an exact command for a specific single band (e.g. "B3"),
    #            overriding the "set" template for that selection.
    #   "auto":  an exact command to restore all bands.
    #   "query": the read-back command (e.g. "AT$QCBANDPREF?"); optional.
    def band_lock(self, bands, templates=None):
        """Lock LTE to ``bands`` (label, list, or 'auto'). Returns a dict.

        {ok, bands, mask, mask_hex, command, status, source}. ``source`` is
        'at' when a command was issued, or 'unconfigured' when no band_at
        template exists for this modem (nothing written).
        """
        mask = lte_band_mask(bands)
        mask_hex = format(mask, "x")
        sel = "auto" if (not bands or str(bands).strip().lower() == "auto") \
            else (bands if isinstance(bands, str) else ",".join(bands))
        out = {"ok": False, "bands": sel, "mask": mask, "mask_hex": mask_hex,
               "command": None, "status": None, "source": "unconfigured"}
        if not templates:
            return out
        # Prefer an exact per-selection template, then "auto", then "set".
        cmd = None
        key = sel if isinstance(sel, str) else None
        if key and key in templates:
            cmd = templates[key]
        elif sel == "auto" and "auto" in templates:
            cmd = templates["auto"]
        elif "set" in templates:
            cmd = templates["set"].format(
                mask=mask, mask_hex=mask_hex, mask_hex0x="0x" + mask_hex)
        if not cmd:
            return out
        res = self.at(cmd, timeout=15)
        out.update(ok=res.ok, command=cmd, status=res.status, source="at")
        return out

    def band_status(self, templates=None):
        """Read the modem's current band preference, if a query template is
        configured. Returns {supported, raw, source}."""
        out = {"supported": list(LTE_BANDS_SUPPORTED), "raw": None,
               "source": "unconfigured"}
        if templates and templates.get("query"):
            res = self.at(templates["query"])
            out["raw"] = res.raw
            out["source"] = "at"
        return out


# ===========================================================================
# Parsing helpers
# ===========================================================================
def _csq_to_bars(rssi):
    if rssi == 99 or rssi < 0:
        return 0
    if rssi >= 20:
        return 5
    if rssi >= 15:
        return 4
    if rssi >= 10:
        return 3
    if rssi >= 5:
        return 2
    if rssi >= 1:
        return 1
    return 0


def _parse_cmgl_pdu(lines):
    """Parse +CMGL PDU-mode output into message dicts."""
    out = []
    i = 0
    while i < len(lines):
        header = lines[i]
        m = re.match(r"\+CMGL:\s*(\d+),(\d+),", header)
        if m and i + 1 < len(lines):
            index = int(m.group(1))
            stat = int(m.group(2))
            pdu = lines[i + 1].strip()
            out.append(_decode_sms_entry(index, stat, pdu))
            i += 2
        else:
            i += 1
    return out


def _parse_cmgr_pdu(lines, index):
    out = []
    i = 0
    while i < len(lines):
        header = lines[i]
        m = re.match(r"\+CMGR:\s*(\d+),", header)
        if m and i + 1 < len(lines):
            stat = int(m.group(1))
            pdu = lines[i + 1].strip()
            out.append(_decode_sms_entry(index, stat, pdu))
            i += 2
        else:
            i += 1
    return out


def _decode_sms_entry(index, stat, pdu):
    entry = {"index": index, "stat": stat, "pdu": pdu,
             "sender": None, "text": None, "timestamp": None, "concat": None}
    try:
        decoded = smspdu.decode_deliver(pdu)
        entry["sender"] = decoded.get("sender")
        entry["text"] = decoded.get("text")
        entry["timestamp"] = decoded.get("timestamp")
        entry["concat"] = decoded.get("concat")
    except Exception as exc:  # malformed PDU - keep the raw, note the error
        entry["text"] = None
        entry["error"] = "decode failed: %s" % exc
        log.event(30, "sms.decode_failed", index=index, err=str(exc))
    return entry


def _wait_for_prompt(fd, timeout=5):
    """Read until the modem emits the '>' SMS prompt or timeout."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        r, _, _ = select.select([fd], [], [], deadline - time.monotonic())
        if not r:
            return False
        try:
            chunk = os.read(fd, 256)
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            return False
        buf += chunk
        if b">" in buf:
            return True
    return False


def _luhn_ok(imei):
    """Validate the 15-digit IMEI Luhn check digit."""
    digits = [int(c) for c in imei]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# ===========================================================================
# Factory
# ===========================================================================
def build_modem(cfg, mock=False):
    """Create a ModemControl from the ``modem`` config dict.

    ``mock`` (or ``cfg['mock']``) selects the fake transport.
    """
    use_mock = mock or cfg.get("mock", False)
    if use_mock:
        log.event(20, "modem.mock_enabled")
        transport = MockModem(device="(mock)", timeout=cfg.get("timeout", 8.0))
    else:
        transport = Modem(device=cfg.get("device", "/dev/smd11"),
                          timeout=cfg.get("timeout", 8.0))
    return ModemControl(transport)
