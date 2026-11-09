#!/usr/bin/env python3
"""Read the modem's current LTE band preference over QMI (READ-ONLY).

Uses a free raw SMD control channel (smdcntl8, unused by qmuxd) and standard
QMUX/QMI framing to:
  1. QMI_CTL allocate a NAS client id,
  2. QMI_NAS Get System Selection Preference (0x0034) -> print mode_pref,
     band_pref, lte_band_pref bitmasks,
  3. QMI_NAS Get RF Band Info (0x0031) -> print the current serving band,
  4. release the client id.

Writes NOTHING to the modem (no SET, no NV). This is the read step that must
run and be reviewed before any band-preference SET.

    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/qmi_bandpref.py"'
"""
import os
import struct
import select
import time

QMI_CTL = 0x00
QMI_NAS = 0x03
CTL_ALLOC_CID = 0x0022
CTL_RELEASE_CID = 0x0023
NAS_GET_SYS_SEL_PREF = 0x0034
NAS_GET_RF_BAND_INFO = 0x0031

CHANNEL = "/dev/smdcntl8"

# E-UTRA bands by bit position (bit 0 = Band 1). Just the ones we care about.
KNOWN_BANDS = {0: "B1", 1: "B2", 2: "B3", 3: "B4", 4: "B5", 6: "B7",
               7: "B8", 19: "B20", 27: "B28", 29: "B30", 32: "B33",
               37: "B38", 38: "B39", 39: "B40", 40: "B41"}


def decode_bands(mask):
    out = []
    for bit in range(64):
        if mask & (1 << bit):
            out.append(KNOWN_BANDS.get(bit, "band%d" % (bit + 1)))
    return out


class Qmi:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.ctl_txn = 1
        self.svc_txn = 1

    def close(self):
        os.close(self.fd)

    def _send(self, service, client, qmi):
        body = struct.pack('<BBB', 0x00, service, client) + qmi
        pkt = struct.pack('<BH', 0x01, len(body) + 2) + body
        os.write(self.fd, pkt)

    def _frames(self, timeout=2.0):
        end = time.time() + timeout
        buf = bytearray()
        frames = []
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.3)
            if not r:
                if frames:
                    break
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except OSError:
                time.sleep(0.02)
                continue
            if chunk:
                buf += chunk
            while len(buf) >= 3 and buf[0] == 0x01:
                ln = struct.unpack('<H', bytes(buf[1:3]))[0]
                total = ln + 1
                if len(buf) >= total:
                    frames.append(bytes(buf[:total]))
                    del buf[:total]
                else:
                    break
        return frames

    @staticmethod
    def _parse(frame):
        # returns (service, client, msg_id, tlv_bytes)
        service = frame[4]
        client = frame[5]
        qmi = frame[6:]
        if service == QMI_CTL:
            msg_id = struct.unpack('<H', qmi[2:4])[0]
            tlvlen = struct.unpack('<H', qmi[4:6])[0]
            tlvs = qmi[6:6 + tlvlen]
        else:
            msg_id = struct.unpack('<H', qmi[3:5])[0]
            tlvlen = struct.unpack('<H', qmi[5:7])[0]
            tlvs = qmi[7:7 + tlvlen]
        return service, client, msg_id, tlvs

    @staticmethod
    def tlvs(tlv_bytes):
        out = {}
        i = 0
        while i + 3 <= len(tlv_bytes):
            t = tlv_bytes[i]
            ln = struct.unpack('<H', tlv_bytes[i + 1:i + 3])[0]
            out[t] = tlv_bytes[i + 3:i + 3 + ln]
            i += 3 + ln
        return out

    def ctl(self, msg_id, tlvs=b""):
        txn = self.ctl_txn
        self.ctl_txn = (self.ctl_txn % 255) + 1
        qmi = struct.pack('<BBHH', 0x00, txn, msg_id, len(tlvs)) + tlvs
        self._send(QMI_CTL, 0x00, qmi)
        for f in self._frames():
            s, c, m, t = self._parse(f)
            if s == QMI_CTL and m == msg_id:
                return t
        return None

    def nas(self, client, msg_id, tlvs=b""):
        txn = self.svc_txn
        self.svc_txn = (self.svc_txn % 65535) + 1
        qmi = struct.pack('<BHHH', 0x00, txn, msg_id, len(tlvs)) + tlvs
        self._send(QMI_NAS, client, qmi)
        for f in self._frames():
            s, c, m, t = self._parse(f)
            if s == QMI_NAS and m == msg_id:
                return t
        return None


q = Qmi(CHANNEL)
print("QMI over %s (read-only)" % CHANNEL)

# 1. allocate NAS client id  (TLV type=0x01 len=0x0001 value=service)
t = q.ctl(CTL_ALLOC_CID, struct.pack('<BHB', 0x01, 1, QMI_NAS))
cid = None
if t:
    tl = Qmi.tlvs(t)
    if 0x01 in tl and len(tl[0x01]) >= 2:
        cid = tl[0x01][1]
print("  NAS client id:", cid, "(raw tlvs=%s)" % (t.hex() if t else None))

if cid is not None:
    # 2. Get System Selection Preference
    t = q.nas(cid, NAS_GET_SYS_SEL_PREF)
    if t:
        tl = Qmi.tlvs(t)
        print("  SysSelPref TLV ids:", [hex(k) for k in tl])
        if 0x11 in tl:
            print("    mode_pref    = %s" % tl[0x11].hex())
        if 0x12 in tl and len(tl[0x12]) >= 8:
            bp = struct.unpack('<Q', tl[0x12][:8])[0]
            print("    band_pref    = 0x%016x %s" % (bp, decode_bands(bp)))
        if 0x15 in tl and len(tl[0x15]) >= 8:
            lb = struct.unpack('<Q', tl[0x15][:8])[0]
            print("    lte_band_pref= 0x%016x %s" % (lb, decode_bands(lb)))
        # dump any other TLVs for reference
        for k, v in tl.items():
            if k not in (0x11, 0x12, 0x15):
                print("    tlv 0x%02x    = %s" % (k, v.hex()))
    else:
        print("  Get SysSelPref: no response")

    # 3. Current serving band
    t = q.nas(cid, NAS_GET_RF_BAND_INFO)
    if t:
        print("  RF band info tlvs:", {hex(k): v.hex()
                                       for k, v in Qmi.tlvs(t).items()})

    # 4. release
    q.ctl(CTL_RELEASE_CID, struct.pack('<BHBB', 0x01, 2, QMI_NAS, cid))

q.close()
