#!/usr/bin/env python3
"""Find the SET System Selection Preference TLV form this modem accepts.

Our first SET (TLV 0x14 lte_band_pref + 0x17 change_duration) returned
MALFORMED_MSG (err 1). GET works with identical framing, so the TLVs are the
issue. This tries several documented variants and prints the QMI result/error
for each, so we learn the accepted form before committing.

Each variant that FAILS applies nothing. A variant that SUCCEEDS *does* apply
the runtime band-lock (change_duration=power_cycle) — which is the goal; the
script stops and reports it.
"""
import os
import struct
import select
import time

QMI_CTL = 0x00
QMI_NAS = 0x03
CTL_ALLOC_CID = 0x0022
CTL_RELEASE_CID = 0x0023
NAS_SET = 0x0033
NAS_GET = 0x0034
CHANNEL = "/dev/smdcntl8"

MASK = 0x0000008000000095            # B1,B3,B5,B8,B40
PC = 0x00                            # change_duration = power cycle

QMI_ERR = {0: "NONE", 1: "MALFORMED_MSG", 2: "NO_MEMORY", 3: "INTERNAL",
           0x11: "MISSING_ARG", 0x13: "ARG_TOO_LONG", 0x16: "INVALID_TX_ID",
           0x17: "DEVICE_IN_USE", 0x18: "OP_NETWORK_UNSUPPORTED",
           0x19: "OP_DEVICE_UNSUPPORTED", 0x1A: "NO_EFFECT",
           0x31: "NOT_SUPPORTED", 0x48: "INVALID_ARG", 0x5A: "INVALID_ARG"}


def tlv(t, v):
    return struct.pack('<BH', t, len(v)) + v


class Qmi:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.ct = 1
        self.st = 1

    def close(self):
        os.close(self.fd)

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
    def parse(f):
        svc = f[4]
        q = f[6:]
        if svc == QMI_CTL:
            m = struct.unpack('<H', q[2:4])[0]
            tl = struct.unpack('<H', q[4:6])[0]
            return svc, m, q[6:6 + tl]
        m = struct.unpack('<H', q[3:5])[0]
        tl = struct.unpack('<H', q[5:7])[0]
        return svc, m, q[7:7 + tl]

    @staticmethod
    def tlvs(b):
        o = {}
        i = 0
        while i + 3 <= len(b):
            t = b[i]
            ln = struct.unpack('<H', b[i + 1:i + 3])[0]
            o[t] = b[i + 3:i + 3 + ln]
            i += 3 + ln
        return o

    def ctl(self, msg, t=b""):
        x = self.ct
        self.ct = (self.ct % 255) + 1
        self._send(QMI_CTL, 0, struct.pack('<BBHH', 0, x, msg, len(t)) + t)
        for f in self._frames():
            s, m, tt = self.parse(f)
            if s == QMI_CTL and m == msg:
                return tt
        return None

    def nas(self, cli, msg, t=b""):
        x = self.st
        self.st = (self.st % 65535) + 1
        self._send(QMI_NAS, cli, struct.pack('<BHHH', 0, x, msg, len(t)) + t)
        for f in self._frames():
            s, m, tt = self.parse(f)
            if s == QMI_NAS and m == msg:
                return tt
        return None


q = Qmi(CHANNEL)
ct = q.ctl(CTL_ALLOC_CID, struct.pack('<BHB', 0x01, 1, QMI_NAS))
cid = Qmi.tlvs(ct)[0x01][1]
print("NAS cid =", cid)

u64 = struct.pack('<Q', MASK)
dur = tlv(0x17, struct.pack('<B', PC))
variants = [
    ("0x14 only",              tlv(0x14, u64)),
    ("0x14 + 0x17",            tlv(0x14, u64) + dur),
    ("0x10(LTE)+0x14+0x17",    tlv(0x10, struct.pack('<H', 0x0010)) + tlv(0x14, u64) + dur),
    ("0x15 + 0x17",            tlv(0x15, u64) + dur),
    ("0x1D ext(16) + 0x17",    tlv(0x1D, u64 + b'\x00' * 8) + dur),
    ("0x14 + dur=permanent",   tlv(0x14, u64) + tlv(0x17, b'\x01')),
]
for name, body in variants:
    r = q.nas(cid, NAS_SET, body)
    tl = Qmi.tlvs(r) if r else {}
    if 0x02 in tl:
        res, err = struct.unpack('<HH', tl[0x02])
        print("  %-24s -> result=%d err=%d(%s)"
              % (name, res, err, QMI_ERR.get(err, "?")))
        if res == 0:
            print("    *** SUCCESS — this variant applied the band-lock ***")
            break
    else:
        print("  %-24s -> no/again resp=%s" % (name, r.hex() if r else None))
    time.sleep(0.3)

q.ctl(CTL_RELEASE_CID, struct.pack('<BHBB', 0x01, 2, QMI_NAS, cid))
q.close()
