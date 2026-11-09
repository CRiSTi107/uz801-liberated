#!/usr/bin/env python3
"""Unlock everything and watch the modem freely find the best cell (~60s).

Removes the band-lock (all LTE bands) and allows 2G/3G/4G, then reports
registration + serving EARFCN + RSSI + real data every 4s. This is the honest
"can this location/antenna register and carry data at all" test. Leaves the
modem unlocked (stock-like) at the end.

    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/test_auto.py"'
"""
import sys
import time
import socket
import struct
sys.path.insert(0, "/opt/cockpit/api")
import qmi  # noqa: E402

ALL_BANDS = 0x7FFFFFFFFFFFFFFF
MODE_ALL = 0x001C          # GSM | WCDMA | LTE

q = qmi.Qmi()
q.alloc_nas()
ok, res, err = q.set_band_pref(ALL_BANDS)
print("unlock all LTE bands:", ok)
for tid in (0x11, 0x10):
    tl = struct.pack("<BHH", tid, 2, MODE_ALL) + struct.pack("<BHB", 0x17, 1, 0)
    r = q.nas(qmi.NAS_SET_SYS_SEL_PREF, tl)
    t = qmi.Qmi.tlvs(r) if r else {}
    if 0x02 in t and struct.unpack("<HH", t[0x02][:4]) == (0, 0):
        print("allow 2G/3G/4G via TLV 0x%02x" % tid)
        break


def rssi():
    t = q.nas(0x0020, struct.pack("<BHH", 0x10, 2, 0x00EF))
    tl = qmi.Qmi.tlvs(t) if t else {}
    if 1 in tl and len(tl[1]) >= 2:
        return struct.unpack("<b", tl[1][0:1])[0]
    return None


def serving():
    t = q.nas(0x0031)
    tl = qmi.Qmi.tlvs(t) if t else {}
    if 0x01 in tl and len(tl[0x01]) >= 6:
        v = tl[0x01]
        rif = v[1]
        earfcn = struct.unpack("<H", v[4:6])[0]
        return rif, earfcn
    return None, None


RIF = {4: "GSM", 5: "UMTS", 8: "LTE"}
ever = "down"
for i in range(15):
    time.sleep(4)
    reg, _b, _e = q.get_serving()
    rif, earfcn = serving()
    up = False
    for h in ("1.1.1.1", "8.8.8.8", "193.231.252.1"):
        try:
            s = socket.create_connection((h, 53), timeout=4)
            s.close()
            up = True
            break
        except OSError:
            pass
    if up:
        ever = "UP"
    print("  t+%2ds  reg=%-14s rat=%-4s earfcn=%-6s rssi=%s dBm  data=%s"
          % ((i + 1) * 4, reg, RIF.get(rif, rif), earfcn, rssi(),
             "UP" if up else "down"))
q.close()
print("result: data %s. modem left UNLOCKED (all bands, 2G/3G/4G)." % ever)
