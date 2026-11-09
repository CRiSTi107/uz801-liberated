#!/usr/bin/env python3
"""Per-band DATA-holds test: lock each band, wait, test real TCP connectivity.

Answers 'which band actually carries data right now' — including B20/B7 that we
normally exclude as antenna-unusable. For each band: QMI-lock it, dwell, read
registration, then try a TCP connect (port 53) to public + carrier resolvers.
Restores the usable-set lock at the end. Run in the chroot (shares host netns):

    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/data_band_test.py"'
"""
import sys
import time
import socket
sys.path.insert(0, "/opt/cockpit/api")
import qmi  # noqa: E402

BANDS = [("B20", 0x80000), ("B7", 0x40), ("B1", 0x1),
         ("B3", 0x4), ("B8", 0x80), ("B40", 0x8000000000)]
HOSTS = ["1.1.1.1", "8.8.8.8", "193.231.252.1"]
RESTORE = 0x0000008000000095  # B1/B3/B5/B8/B40


def data_up():
    for h in HOSTS:
        try:
            s = socket.create_connection((h, 53), timeout=5)
            s.close()
            return True, h
        except OSError as e:
            last = e
    return False, str(last)


print("per-band data test (lock, dwell 12s, TCP:53 probe):")
for name, mask in BANDS:
    qmi.apply_band(mask)
    time.sleep(12)
    r = qmi.read_band()
    reg = r.get("reg_state")
    serv = (r.get("serving") or {}).get("band")
    up, detail = data_up()
    print("  %-4s lock -> reg=%-11s serving=%-5s data=%s (%s)"
          % (name, reg, serv, "UP" if up else "down", detail))

res = qmi.apply_band(RESTORE)
print("restored lock:", res.get("bands"))
