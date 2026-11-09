#!/usr/bin/env python3
"""Lock LTE bands via QMI NAS Set System Selection Preference (RUNTIME only).

Narrows the modem's lte_band_pref to B1/B3/B5/B8/B40 using change_duration =
POWER_CYCLE, so it is a runtime setting: NO NV write, reverts on reboot, zero
brick risk. Re-apply each boot from the boot-hook.

DEFAULT = DRY RUN: prints the exact QMUX/QMI bytes it would send and the target
band decode, and writes nothing. Pass --apply to actually send the SET, then it
re-reads the preference and the serving band to confirm.

    # dry run (safe, default):
    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/qmi_bandlock.py"'
    # apply (runtime band-lock):
    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/qmi_bandlock.py --apply"'
"""
import os
import sys
import struct
import select
import time

QMI_CTL = 0x00
QMI_NAS = 0x03
CTL_ALLOC_CID = 0x0022
CTL_RELEASE_CID = 0x0023
NAS_SET_SYS_SEL_PREF = 0x0033
NAS_GET_SYS_SEL_PREF = 0x0034
NAS_GET_RF_BAND_INFO = 0x0031

CHANNEL = "/dev/smdcntl8"

# Keep only the dongle's usable FDD/TDD bands.
KEEP_BITS = [0, 2, 4, 7, 39]      # B1, B3, B5, B8, B40
TARGET_MASK = 0
for _b in KEEP_BITS:
    TARGET_MASK |= (1 << _b)

CHANGE_POWER_CYCLE = 0x00         # runtime; reverts on reboot (NOT 0x01=permanent)

KNOWN = {0: "B1", 2: "B3", 4: "B5", 7: "B8", 6: "B7", 19: "B20", 39: "B40"}


def decode(mask):
    return [KNOWN.get(b, "band%d" % (b + 1)) for b in range(64) if mask & (1 << b)]


class Qmi:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.ctl_txn = 1
        self.svc_txn = 1

    def close(self):
        os.close(self.fd)

    def _send(self, service, client, qmi):
        body = struct.pack('<BBB', 0x00, service, client) + qmi
        os.write(self.fd, struct.pack('<BH', 0x01, len(body) + 2) + body)

    def _frames(self, timeout=3.0):
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
                ln = struct.unpack('<H', bytes(buf[1:3]))[0] + 1
                if len(buf) >= ln:
                    frames.append(bytes(buf[:ln]))
                    del buf[:ln]
                else:
                    break
        return frames

    @staticmethod
    def _parse(frame):
        service = frame[4]
        qmi = frame[6:]
        if service == QMI_CTL:
            msg = struct.unpack('<H', qmi[2:4])[0]
            tl = struct.unpack('<H', qmi[4:6])[0]
            return service, msg, qmi[6:6 + tl]
        msg = struct.unpack('<H', qmi[3:5])[0]
        tl = struct.unpack('<H', qmi[5:7])[0]
        return service, msg, qmi[7:7 + tl]

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
        txn = self.ctl_txn
        self.ctl_txn = (self.ctl_txn % 255) + 1
        self._send(QMI_CTL, 0x00,
                   struct.pack('<BBHH', 0x00, txn, msg_id, len(tlvs)) + tlvs)
        for f in self._frames():
            s, m, t = self._parse(f)
            if s == QMI_CTL and m == msg_id:
                return t
        return None

    def nas_build(self, client, msg_id, tlvs):
        txn = self.svc_txn
        self.svc_txn = (self.svc_txn % 65535) + 1
        qmi = struct.pack('<BHHH', 0x00, txn, msg_id, len(tlvs)) + tlvs
        body = struct.pack('<BBB', 0x00, QMI_NAS, client) + qmi
        return struct.pack('<BH', 0x01, len(body) + 2) + body

    def nas(self, client, msg_id, tlvs=b""):
        os.write(self.fd, self.nas_build(client, msg_id, tlvs))
        for f in self._frames():
            s, m, t = self._parse(f)
            if s == QMI_NAS and m == msg_id:
                return t
        return None


def main():
    apply = "--apply" in sys.argv
    # SET request TLVs: 0x15 lte_band_pref (u64), 0x17 change_duration (u8).
    # NOTE: this modem (Longsung/Huawei) uses TLV 0x15 for the SET band pref,
    # not the libqmi-standard 0x14 (0x14/0x10 both return MALFORMED_MSG here).
    set_tlvs = (struct.pack('<BHQ', 0x15, 8, TARGET_MASK)
                + struct.pack('<BHB', 0x17, 1, CHANGE_POWER_CYCLE))

    print("Target lte_band_pref = 0x%016x  %s" % (TARGET_MASK, decode(TARGET_MASK)))
    print("change_duration      = 0x%02x (power-cycle / runtime, reverts on reboot)"
          % CHANGE_POWER_CYCLE)

    q = Qmi(CHANNEL)
    cid_t = q.ctl(CTL_ALLOC_CID, struct.pack('<BHB', 0x01, 1, QMI_NAS))
    cid = Qmi.tlvs(cid_t)[0x01][1] if cid_t and 0x01 in Qmi.tlvs(cid_t) else None
    print("NAS client id        =", cid)
    if cid is None:
        print("could not allocate NAS client; aborting")
        q.close()
        return

    frame = q.nas_build(cid, NAS_SET_SYS_SEL_PREF, set_tlvs)
    print("SET frame (QMUX/QMI) = %s" % frame.hex())

    if not apply:
        print("\nDRY RUN — nothing sent. Re-run with --apply to write (runtime).")
        q.ctl(CTL_RELEASE_CID, struct.pack('<BHBB', 0x01, 2, QMI_NAS, cid))
        q.close()
        return

    # --- APPLY ---
    # rebuild with a fresh txn and send
    resp = q.nas(cid, NAS_SET_SYS_SEL_PREF, set_tlvs)
    rt = Qmi.tlvs(resp) if resp else {}
    res = struct.unpack('<HH', rt[0x02])[:2] if 0x02 in rt else (None, None)
    print("SET result TLV       = %s  (0,0 = SUCCESS)" % (res,))

    time.sleep(2)
    g = q.nas(cid, NAS_GET_SYS_SEL_PREF)
    gt = Qmi.tlvs(g) if g else {}
    if 0x15 in gt:
        lb = struct.unpack('<Q', gt[0x15][:8])[0]
        print("read-back lte_band   = 0x%016x  %s" % (lb, decode(lb)))
    rf = q.nas(cid, NAS_GET_RF_BAND_INFO)
    if rf:
        print("serving band info    = %s"
              % {hex(k): v.hex() for k, v in Qmi.tlvs(rf).items()})

    q.ctl(CTL_RELEASE_CID, struct.pack('<BHBB', 0x01, 2, QMI_NAS, cid))
    q.close()


if __name__ == "__main__":
    main()
