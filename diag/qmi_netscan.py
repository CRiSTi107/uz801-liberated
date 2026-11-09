#!/usr/bin/env python3
"""Diagnose why the modem won't register: QMI network scan + serving + signal.

Over the free smdcntl8 QMI control point (read-only, no NV writes):
  - NAS Perform Network Scan (0x0021): which operators (PLMNs) are visible and
    their status (current / available / forbidden) — the decisive test for a
    "registration denied" (reject) vs a "no signal" problem.
  - NAS Get Serving System (0x0024): registration state + roaming.
  - NAS Get Signal Strength (0x0020): RSSI on the serving RAT.

    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/qmi_netscan.py"'
"""
import os
import struct
import select
import time

CH = "/dev/smdcntl8"
QMI_CTL, QMI_NAS = 0x00, 0x03
CTL_ALLOC, CTL_RELEASE = 0x0022, 0x0023
NAS_SCAN = 0x0021
NAS_SERVING = 0x0024
NAS_SIGNAL = 0x0020

REG = {0: "not_registered", 1: "registered", 2: "searching", 3: "DENIED",
       4: "unknown"}


class Qmi:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.ct = 1
        self.st = 1
        self.cid = None

    def close(self):
        os.close(self.fd)

    def _send(self, svc, cli, qmi):
        b = struct.pack('<BBB', 0, svc, cli) + qmi
        os.write(self.fd, struct.pack('<BH', 1, len(b) + 2) + b)

    def _frames(self, timeout):
        end = time.time() + timeout
        buf = bytearray()
        fr = []
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.5)
            if not r:
                if fr:
                    break
                continue
            try:
                c = os.read(self.fd, 8192)
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
    def _p(f):
        svc = f[4]
        q = f[6:]
        if svc == QMI_CTL:
            return svc, struct.unpack('<H', q[2:4])[0], q[6:6 + struct.unpack('<H', q[4:6])[0]]
        return svc, struct.unpack('<H', q[3:5])[0], q[7:7 + struct.unpack('<H', q[5:7])[0]]

    @staticmethod
    def tl(b):
        o = {}
        i = 0
        while i + 3 <= len(b):
            t = b[i]
            n = struct.unpack('<H', b[i + 1:i + 3])[0]
            o[t] = b[i + 3:i + 3 + n]
            i += 3 + n
        return o

    def ctl(self, m, t=b""):
        x = self.ct
        self.ct = (self.ct % 255) + 1
        self._send(QMI_CTL, 0, struct.pack('<BBHH', 0, x, m, len(t)) + t)
        for f in self._frames(3):
            s, mm, tt = self._p(f)
            if s == QMI_CTL and mm == m:
                return tt
        return None

    def nas(self, m, t=b"", timeout=5):
        x = self.st
        self.st = (self.st % 65535) + 1
        self._send(QMI_NAS, self.cid, struct.pack('<BHHH', 0, x, m, len(t)) + t)
        for f in self._frames(timeout):
            s, mm, tt = self._p(f)
            if s == QMI_NAS and mm == m:
                return tt
        return None


def main():
    q = Qmi(CH)
    t = q.ctl(CTL_ALLOC, struct.pack('<BHB', 1, 1, QMI_NAS))
    q.cid = Qmi.tl(t)[1][1]
    print("NAS cid", q.cid)

    # serving system
    ss = Qmi.tl(q.nas(NAS_SERVING) or b"")
    if 1 in ss and len(ss[1]) >= 5:
        reg = ss[1][0]
        roam = ss[1][4] if len(ss[1]) >= 5 else None
        print("serving: reg=%s roaming=%s" % (REG.get(reg, reg), roam))
    # current PLMN if present (TLV 0x12 = current PLMN: mcc,mnc,desc)
    if 0x12 in ss and len(ss[0x12]) >= 5:
        mcc, mnc = struct.unpack('<HH', ss[0x12][:4])
        print("current PLMN: %03d-%02d" % (mcc, mnc))

    # signal
    sg = Qmi.tl(q.nas(NAS_SIGNAL, struct.pack('<BHH', 0x10, 2, 0x00EF)) or b"")
    if 1 in sg and len(sg[1]) >= 2:
        rssi = struct.unpack('<b', sg[1][0:1])[0]
        rif = sg[1][1]
        print("signal: rssi=%ddBm radio_if=%d" % (rssi, rif))
    else:
        print("signal tlvs:", {hex(k): v.hex() for k, v in sg.items()})

    # network scan (can take 60-120s)
    print("network scan (up to ~120s)...")
    ns = Qmi.tl(q.nas(NAS_SCAN, timeout=150) or b"")
    if 0x10 in ns:
        b = ns[0x10]
        n = struct.unpack('<H', b[:2])[0]
        i = 2
        print("  %d networks visible:" % n)
        for _ in range(n):
            if i + 6 > len(b):
                break
            mcc, mnc, status = struct.unpack('<HHB', b[i:i + 5])
            dlen = b[i + 5]
            desc = b[i + 6:i + 6 + dlen].decode('latin1', 'replace')
            i += 6 + dlen
            in_use = status & 0x3
            forbid = (status >> 4) & 0x3
            su = {1: "current", 2: "available"}.get(in_use, "?")
            fb = {1: "FORBIDDEN", 2: "allowed"}.get(forbid, "?")
            print("   %03d-%02d  %-10s  %-9s  %s" % (mcc, mnc, su, fb, desc))
    else:
        print("  no network-info TLV; tlvs:",
              {hex(k): len(v) for k, v in ns.items()})

    q.ctl(CTL_RELEASE, struct.pack('<BHBB', 1, 2, QMI_NAS, q.cid))
    q.close()


if __name__ == "__main__":
    main()
