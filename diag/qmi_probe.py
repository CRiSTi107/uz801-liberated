#!/usr/bin/env python3
"""Minimal QMUX/QMI probe over a raw SMD control channel (READ-ONLY).

qmuxd holds /dev/smdcntl0..7; smdcntl8..11 are root-owned and unused by qmuxd.
This opens the free ones and speaks the standard QMUX/QMI framing directly to
the modem to find a channel that answers QMI_CTL get_version (0x0021). Proves a
usable parallel QMI control point before any NAS band-preference work.

Nothing is written to the modem NV; this only reads version info.

Run in the Alpine chroot as root (smdcntl* bind-mounted or chroot shares /dev):
    adb shell '/system/bin/sh /data/linux/enter.sh "python3 /opt/diag/qmi_probe.py"'
or straight on the Android host:
    adb shell 'python3 /path/qmi_probe.py'   # if python3 present on host
"""
import os
import struct
import select
import time

QMI_CTL = 0x00
CTL_GET_VERSION = 0x0021


def qmux_wrap(service, client, qmi):
    # flags=0x00 (message from control point / host)
    body = struct.pack('<BBB', 0x00, service, client) + qmi
    length = len(body) + 2            # length counts itself (2) + body
    return struct.pack('<BH', 0x01, length) + body


def ctl_msg(msg_id, tlvs=b"", txn=1):
    qmi = struct.pack('<BBHH', 0x00, txn, msg_id, len(tlvs)) + tlvs
    return qmux_wrap(QMI_CTL, 0x00, qmi)


def probe(path, timeout=2.0):
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError as e:
        return "open failed: %s" % e
    try:
        pkt = ctl_msg(CTL_GET_VERSION)
        try:
            os.write(fd, pkt)
        except OSError as e:
            return "write failed: %s" % e
        end = time.time() + timeout
        buf = bytearray()
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.3)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                time.sleep(0.02)
                continue
            if chunk:
                buf += chunk
                break
        if not buf:
            return "no response (sent %s)" % pkt.hex()
        return "RESP %d bytes: %s" % (len(buf), bytes(buf)[:64].hex())
    finally:
        os.close(fd)


print("QMI_CTL get_version probe on free smdcntl channels (read-only):")
for ch in (8, 9, 10, 11):
    path = "/dev/smdcntl%d" % ch
    print("  %-18s -> %s" % (path, probe(path)))
