#!/usr/bin/env python3
"""QMI band control for the UZ801 modem (cockpit backend + boot applier).

The modem's DIAG interface is info-only (EFS/NV access disabled), so band
selection is done over QMI. qmuxd owns /dev/smdcntl0..7; smdcntl8 is free, so we
open it and speak standard QMUX/QMI directly to the modem.

This modem (Longsung/Huawei MSM8916) takes the lte_band_pref for SET at
QMI_NAS Set System Selection Preference (0x0033) **TLV 0x15** (not the
libqmi-standard 0x14, which returns MALFORMED_MSG), with change_duration
TLV 0x17 = 0x00 (POWER_CYCLE = runtime, reverts on reboot, no NV write).

Used by the cockpit (ep_band_get/ep_band_set) and runnable as a CLI so the
boot-hook can re-apply the saved lock:
    python3 qmi.py read [channel]
    python3 qmi.py apply <hexmask> [channel]      # e.g. apply 0x0000008000000095
    python3 qmi.py apply-saved [maskfile] [channel]
"""
import os
import sys
import struct
import select
import time

DEFAULT_CHANNEL = "/dev/smdcntl8"
DEFAULT_MASKFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "bandlock.mask")

QMI_CTL = 0x00
QMI_NAS = 0x03
CTL_ALLOC_CID = 0x0022
CTL_RELEASE_CID = 0x0023
NAS_SET_SYS_SEL_PREF = 0x0033
NAS_GET_SYS_SEL_PREF = 0x0034
NAS_GET_RF_BAND_INFO = 0x0031
NAS_GET_SERVING_SYSTEM = 0x0024

# SET lte_band_pref TLV id is 0x15 on THIS modem (not the standard 0x14).
TLV_SET_LTE_BAND = 0x15
TLV_CHANGE_DURATION = 0x17
CHANGE_POWER_CYCLE = 0x00            # runtime; 0x01 = permanent (NV) -- avoid

# E-UTRA band bit positions we care about (bit N-1 = band N).
BAND_BITS = {"B1": 0, "B3": 2, "B5": 4, "B8": 7, "B40": 39,
             "B7": 6, "B20": 19}
BIT_BANDS = {v: k for k, v in BAND_BITS.items()}
SUPPORTED = ["B1", "B3", "B5", "B8", "B40"]

REG_STATE = {0: "not_registered", 1: "registered", 2: "searching",
             3: "denied", 4: "unknown"}


def mask_to_bands(mask):
    """Return the known band labels set in an LTE band bitmask."""
    out = []
    for bit in range(64):
        if mask & (1 << bit):
            out.append(BIT_BANDS.get(bit, "band%d" % (bit + 1)))
    return out


def bands_to_mask(bands):
    """'auto'/None/[] -> all SUPPORTED; a label or list -> bitmask int."""
    if not bands or (isinstance(bands, str) and bands.strip().lower() == "auto"):
        bands = SUPPORTED
    if isinstance(bands, str):
        bands = [bands]
    mask = 0
    for b in bands:
        b = str(b).strip().upper()
        if not b.startswith("B"):
            b = "B" + b
        if b in BAND_BITS:
            mask |= 1 << BAND_BITS[b]
    return mask


def earfcn_band(earfcn):
    """Rough E-UTRA DL EARFCN -> band label (the few bands in play here)."""
    if earfcn is None:
        return None
    ranges = [("B1", 0, 600), ("B3", 1200, 1950), ("B5", 2400, 2650),
              ("B7", 2750, 3450), ("B8", 3450, 3800), ("B20", 6150, 6450),
              ("B40", 38650, 39650)]
    for name, lo, hi in ranges:
        if lo <= earfcn < hi:
            return name
    return None


class Qmi:
    """Minimal QMUX/QMI control point over a raw SMD control channel."""

    def __init__(self, path=DEFAULT_CHANNEL):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.ct = 1
        self.st = 1
        self.cid = None

    def close(self):
        try:
            if self.cid is not None:
                self.ctl(CTL_RELEASE_CID,
                         struct.pack('<BHBB', 0x01, 2, QMI_NAS, self.cid))
        except OSError:
            pass
        os.close(self.fd)

    # -- framing -----------------------------------------------------------
    def _send(self, svc, cli, qmi):
        body = struct.pack('<BBB', 0, svc, cli) + qmi
        os.write(self.fd, struct.pack('<BH', 1, len(body) + 2) + body)

    def _frames(self, timeout=3.0):
        end = time.time() + timeout
        buf = bytearray()
        fr = []
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.3)
            if not r:
                if fr:
                    break
                continue
            try:
                c = os.read(self.fd, 4096)
            except OSError:
                time.sleep(0.02)
                continue
            if c:
                buf += c
            while len(buf) >= 3 and buf[0] == 1:
                ln = struct.unpack('<H', bytes(buf[1:3]))[0] + 1
                if len(buf) >= ln:
                    fr.append(bytes(buf[:ln]))
                    del buf[:ln]
                else:
                    break
        return fr

    @staticmethod
    def _parse(f):
        svc = f[4]
        q = f[6:]
        if svc == QMI_CTL:
            msg = struct.unpack('<H', q[2:4])[0]
            tl = struct.unpack('<H', q[4:6])[0]
            return svc, msg, q[6:6 + tl]
        msg = struct.unpack('<H', q[3:5])[0]
        tl = struct.unpack('<H', q[5:7])[0]
        return svc, msg, q[7:7 + tl]

    @staticmethod
    def tlvs(b):
        out = {}
        i = 0
        while i + 3 <= len(b):
            t = b[i]
            ln = struct.unpack('<H', b[i + 1:i + 3])[0]
            out[t] = b[i + 3:i + 3 + ln]
            i += 3 + ln
        return out

    def ctl(self, msg_id, tlvs=b""):
        txn = self.ct
        self.ct = (self.ct % 255) + 1
        self._send(QMI_CTL, 0,
                   struct.pack('<BBHH', 0, txn, msg_id, len(tlvs)) + tlvs)
        for f in self._frames():
            s, m, t = self._parse(f)
            if s == QMI_CTL and m == msg_id:
                return t
        return None

    def nas(self, msg_id, tlvs=b""):
        txn = self.st
        self.st = (self.st % 65535) + 1
        self._send(QMI_NAS, self.cid,
                   struct.pack('<BHHH', 0, txn, msg_id, len(tlvs)) + tlvs)
        for f in self._frames():
            s, m, t = self._parse(f)
            if s == QMI_NAS and m == msg_id:
                return t
        return None

    # -- high level --------------------------------------------------------
    def alloc_nas(self):
        t = self.ctl(CTL_ALLOC_CID, struct.pack('<BHB', 0x01, 1, QMI_NAS))
        tl = self.tlvs(t) if t else {}
        if 0x01 in tl and len(tl[0x01]) >= 2:
            self.cid = tl[0x01][1]
        return self.cid

    def get_band_pref(self):
        t = self.nas(NAS_GET_SYS_SEL_PREF)
        tl = self.tlvs(t) if t else {}
        if 0x15 in tl and len(tl[0x15]) >= 8:
            return struct.unpack('<Q', tl[0x15][:8])[0]
        return None

    def get_serving(self):
        """Return (reg_state_str, band_label, earfcn)."""
        reg = None
        t = self.nas(NAS_GET_SERVING_SYSTEM)
        tl = self.tlvs(t) if t else {}
        if 0x01 in tl and len(tl[0x01]) >= 1:
            reg = REG_STATE.get(tl[0x01][0], tl[0x01][0])
        band = earfcn = None
        t = self.nas(NAS_GET_RF_BAND_INFO)
        tl = self.tlvs(t) if t else {}
        if 0x01 in tl and len(tl[0x01]) >= 6:
            v = tl[0x01]
            earfcn = struct.unpack('<H', v[4:6])[0]
            band = earfcn_band(earfcn)
        return reg, band, earfcn

    def set_band_pref(self, mask, permanent=False):
        dur = 0x01 if permanent else CHANGE_POWER_CYCLE
        tlvs = (struct.pack('<BHQ', TLV_SET_LTE_BAND, 8, mask)
                + struct.pack('<BHB', TLV_CHANGE_DURATION, 1, dur))
        t = self.nas(NAS_SET_SYS_SEL_PREF, tlvs)
        tl = self.tlvs(t) if t else {}
        if 0x02 in tl and len(tl[0x02]) >= 4:
            result, error = struct.unpack('<HH', tl[0x02][:4])
            return result == 0, result, error
        return False, None, None


# ===========================================================================
# Convenience API used by the cockpit server
# ===========================================================================
def read_band(channel=DEFAULT_CHANNEL):
    """Return current band-lock + serving state, or {'ok': False, ...}."""
    try:
        q = Qmi(channel)
    except OSError as exc:
        return {"ok": False, "error": "open %s: %s" % (channel, exc)}
    try:
        if q.alloc_nas() is None:
            return {"ok": False, "error": "could not allocate NAS client"}
        mask = q.get_band_pref()
        reg, band, earfcn = q.get_serving()
        return {
            "ok": True,
            "mask": mask,
            "mask_hex": ("0x%016x" % mask) if mask is not None else None,
            "bands": mask_to_bands(mask) if mask is not None else [],
            "reg_state": reg,
            "serving": {"band": band, "earfcn": earfcn},
        }
    finally:
        q.close()


def apply_band(mask, channel=DEFAULT_CHANNEL, permanent=False):
    """Apply an LTE band mask over QMI. Returns a result dict."""
    try:
        q = Qmi(channel)
    except OSError as exc:
        return {"ok": False, "error": "open %s: %s" % (channel, exc)}
    try:
        if q.alloc_nas() is None:
            return {"ok": False, "error": "could not allocate NAS client"}
        ok, result, error = q.set_band_pref(mask, permanent=permanent)
        out = {"ok": ok, "result": result, "error": error,
               "mask": mask, "mask_hex": "0x%016x" % mask,
               "bands": mask_to_bands(mask)}
        if ok:
            reg, band, earfcn = q.get_serving()
            out["serving"] = {"band": band, "earfcn": earfcn}
            out["reg_state"] = reg
        return out
    finally:
        q.close()


def scan_bands(bands=None, channel=DEFAULT_CHANNEL, dwell=4.0, measure=None,
               restore=True):
    """Sweep each usable band: briefly lock it, see if a cell appears + how
    strong, then restore the pre-scan lock. Disruptive (~dwell*len(bands)s).

    ``measure`` is an optional callable returning a signal dict shaped like
    modem.signal() ({rsrp, earfcn, band, cells:[{band,earfcn,rsrp}]}); it is
    read once per band (while that band is locked) to get the RSRP.
    """
    bands = bands or SUPPORTED
    try:
        q = Qmi(channel)
    except OSError as exc:
        return {"ok": False, "error": "open %s: %s" % (channel, exc)}
    try:
        if q.alloc_nas() is None:
            return {"ok": False, "error": "could not allocate NAS client"}
        original = q.get_band_pref()
        results = []
        for label in bands:
            mask = bands_to_mask(label)
            if mask == 0:
                continue
            ok, _, _ = q.set_band_pref(mask)
            if not ok:                       # one retry on a transient reject
                time.sleep(0.5)
                ok, _, _ = q.set_band_pref(mask)
            if not ok:
                results.append({"band": label, "error": "set failed",
                                "available": False})
                continue
            time.sleep(dwell)
            reg, _sband, _earfcn = q.get_serving()
            # Availability is judged ONLY by a measured cell that actually
            # belongs to the locked band (the serving EARFCN from get_serving
            # is unreliable mid-retune), or a full registration.
            rsrp = None
            earfcn = None
            if measure is not None:
                try:
                    sig = measure() or {}
                except Exception:  # noqa: BLE001
                    sig = {}
                same = [c for c in (sig.get("cells") or [])
                        if c.get("band") == label and c.get("rsrp") is not None]
                if same:
                    best = max(same, key=lambda c: c["rsrp"])
                    rsrp, earfcn = best["rsrp"], best.get("earfcn")
                elif sig.get("band") == label and sig.get("rsrp") is not None:
                    rsrp, earfcn = sig["rsrp"], sig.get("earfcn")
            available = bool(rsrp is not None or reg == "registered")
            results.append({"band": label, "reg_state": reg,
                            "earfcn": earfcn, "rsrp": rsrp,
                            "available": available})
        target = original if restore else None
        if target:
            q.set_band_pref(target)
        return {"ok": True, "results": results,
                "restored_mask_hex": ("0x%016x" % target) if target else None,
                "restored_bands": mask_to_bands(target) if target else []}
    finally:
        q.close()


def save_mask(mask, path=DEFAULT_MASKFILE):
    with open(path, "w") as fh:
        fh.write("0x%016x\n" % mask)


def load_mask(path=DEFAULT_MASKFILE):
    try:
        with open(path) as fh:
            s = fh.read().strip()
        return int(s, 16) if s else None
    except (OSError, ValueError):
        return None


# ===========================================================================
# CLI (for the boot-hook / manual use)
# ===========================================================================
def _main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "read":
        ch = argv[1] if len(argv) > 1 else DEFAULT_CHANNEL
        print(read_band(ch))
        return 0
    if cmd == "apply":
        if len(argv) < 2:
            print("usage: qmi.py apply <hexmask> [channel]")
            return 2
        mask = int(argv[1], 16)
        ch = argv[2] if len(argv) > 2 else DEFAULT_CHANNEL
        res = apply_band(mask, ch)
        print(res)
        return 0 if res.get("ok") else 1
    if cmd == "apply-saved":
        mf = argv[1] if len(argv) > 1 else DEFAULT_MASKFILE
        ch = argv[2] if len(argv) > 2 else DEFAULT_CHANNEL
        mask = load_mask(mf)
        if mask is None:
            print("no saved mask at %s; nothing to apply" % mf)
            return 0
        res = apply_band(mask, ch)
        print(res)
        return 0 if res.get("ok") else 1
    print("unknown command: %s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
