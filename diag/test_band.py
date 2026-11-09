#!/usr/bin/env python3
"""Lock ONE band and watch registration + RSSI + real data for ~40s.

For antenna testing: lock the band your dipole is cut for, then see if it
registers and actually carries data. Leaves the band locked at the end.

Usage (in the chroot):
    python3 test_band.py [B20|B5|B1|B3|B8|B40|B7 | 0x<mask>]   (default B20)
"""
import sys
import time
import socket
import struct
sys.path.insert(0, "/opt/cockpit/api")
import qmi  # noqa: E402

BANDS = {"B1": 0x1, "B3": 0x4, "B5": 0x10, "B7": 0x40, "B8": 0x80,
         "B20": 0x80000, "B40": 0x8000000000}

arg = (sys.argv[1] if len(sys.argv) > 1 else "B20").strip()
mask = int(arg, 16) if arg.lower().startswith("0x") else BANDS.get(arg.upper())
if mask is None:
    print("unknown band %r (use %s or 0x<mask>)" % (arg, "/".join(BANDS)))
    sys.exit(2)

print("locking %s (mask=0x%x) ..." % (arg.upper(), mask))
print("  set:", qmi.apply_band(mask).get("ok"))

q = qmi.Qmi()
q.alloc_nas()


def rssi():
    t = q.nas(0x0020, struct.pack("<BHH", 0x10, 2, 0x00EF))
    tl = qmi.Qmi.tlvs(t) if t else {}
    if 1 in tl and len(tl[1]) >= 2:
        return struct.unpack("<b", tl[1][0:1])[0]
    return None


def data_up():
    for h in ("1.1.1.1", "8.8.8.8", "193.231.252.1"):
        try:
            s = socket.create_connection((h, 53), timeout=4)
            s.close()
            return True
        except OSError:
            pass
    return False


best = "down"
for i in range(10):
    time.sleep(4)
    reg, band, earfcn = q.get_serving()
    up = data_up()
    if up:
        best = "UP"
    print("  t+%2ds  reg=%-11s serving=%-4s rssi=%s dBm  data=%s"
          % ((i + 1) * 4, reg, band or "-", rssi(), "UP" if up else "down"))
q.close()
print("result: data %s on %s. (band left locked to %s)"
      % (best, arg.upper(), arg.upper()))
