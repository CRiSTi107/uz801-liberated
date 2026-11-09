#!/usr/bin/env python3
"""Try 2G/3G fallback: the modem is LTE-only; 2G/3G have far better uplink link
budget. Set mode_pref to GSM+WCDMA+LTE (runtime) and watch if data comes up.

Read-only except a runtime mode_pref change (change_duration=power-cycle, no NV;
reverts on reboot). Restores LTE-only at the end unless data came up.

    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/rat_fallback_test.py"'
"""
import sys
import time
import socket
import struct
sys.path.insert(0, "/opt/cockpit/api")
import qmi  # noqa: E402

MODE_GWL = 0x001C   # GSM(0x4) | WCDMA(0x8) | LTE(0x10)
MODE_LTE = 0x0010
RADIO_IF = {4: "GSM", 5: "UMTS", 8: "LTE", 9: "TD-SCDMA"}


def set_mode(q, mode):
    for tid in (0x11, 0x10):
        tl = (struct.pack('<BHH', tid, 2, mode)
              + struct.pack('<BHB', 0x17, 1, 0x00))
        r = q.nas(qmi.NAS_SET_SYS_SEL_PREF, tl)
        t = qmi.Qmi.tlvs(r) if r else {}
        if 0x02 in t and struct.unpack('<HH', t[0x02][:4]) == (0, 0):
            return tid
    return None


def serving_rat(q):
    t = q.nas(0x0031)  # get rf band info
    tl = qmi.Qmi.tlvs(t) if t else {}
    if 0x01 in tl and len(tl[0x01]) >= 2:
        return RADIO_IF.get(tl[0x01][1], tl[0x01][1])
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


q = qmi.Qmi()
q.alloc_nas()
cur = qmi.Qmi.tlvs(q.nas(qmi.NAS_GET_SYS_SEL_PREF) or b"")
print("current mode_pref:", cur.get(0x11, b"").hex() or "?")

used = set_mode(q, MODE_GWL)
print("set mode GSM+WCDMA+LTE via TLV 0x%s:" % (format(used, '02x') if used else "FAILED"))

ever_up = False
for i in range(8):            # observe ~48s
    time.sleep(6)
    reg, band, earfcn = q.get_serving()
    rat = serving_rat(q)
    up = data_up()
    ever_up = ever_up or up
    print("  t+%2ds  reg=%-11s rat=%-5s band=%-4s data=%s"
          % ((i + 1) * 6, reg, rat, band or "-", "UP" if up else "down"))

if ever_up:
    print(">>> DATA CAME UP with 2G/3G fallback allowed — leaving mode = GSM+WCDMA+LTE")
else:
    set_mode(q, MODE_LTE)
    print(">>> no data; restored mode = LTE-only")
q.close()
