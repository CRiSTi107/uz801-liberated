#!/usr/bin/env python3
"""Minimal Qualcomm DIAG transport probe for /dev/diag (msm, 3.10 kernel).
READ-ONLY: sends a DIAG Version request (cmd 0x00) and the Extended Build ID
(cmd 0x7C) and prints any response. Proves whether we can do request/response
over /dev/diag from userspace before any EFS/NV work. No writes to the modem NV.

Run inside the Alpine chroot (python3), as root:
    /system/bin/sh /data/linux/enter.sh 'python3 /opt/diag/qcdiag_probe.py'
"""
import os, fcntl, struct, select, time, sys

DIAG = "/dev/diag"
DIAG_IOCTL_SWITCH_LOGGING = 7
MEMORY_DEVICE_MODE = 2
USER_SPACE_DATA_TYPE = 0x00000020

def crc16(data):  # HDLC FCS: reflected poly 0x1021 (0x8408), init 0xFFFF, xorout 0xFFFF
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if (crc & 1) else (crc >> 1)
    return crc ^ 0xFFFF

def hdlc_encode(payload):
    frame = bytes(payload) + struct.pack('<H', crc16(payload))
    out = bytearray()
    for b in frame:
        if b in (0x7E, 0x7D):
            out += bytes([0x7D, b ^ 0x20])
        else:
            out.append(b)
    out.append(0x7E)
    return bytes(out)

def hdlc_decode(data):
    if not data:
        return b""
    if data[-1] == 0x7E:
        data = data[:-1]
    out = bytearray(); esc = False
    for b in data:
        if esc:
            out.append(b ^ 0x20); esc = False
        elif b == 0x7D:
            esc = True
        else:
            out.append(b)
    return bytes(out[:-2]) if len(out) >= 2 else bytes(out)  # drop CRC

def try_switch_logging(fd):
    # Older msm: int arg. Newer: struct. Try several forms.
    attempts = [
        ("int MEMORY_DEVICE_MODE", MEMORY_DEVICE_MODE),
        ("struct(mode,0,-1,0)", struct.pack('<iiii', MEMORY_DEVICE_MODE, 0, -1, 0)),
        ("struct(mode,pid,-1)", struct.pack('<iii', MEMORY_DEVICE_MODE, os.getpid(), -1)),
    ]
    for name, arg in attempts:
        try:
            fcntl.ioctl(fd, DIAG_IOCTL_SWITCH_LOGGING, arg)
            print("  SWITCH_LOGGING ok via %s" % name); return True
        except Exception as e:
            print("  SWITCH_LOGGING %s failed: %s" % (name, e))
    return False

def send_and_read(fd, cmd, label, prefix=True):
    enc = hdlc_encode(cmd)
    pkt = (struct.pack('<I', USER_SPACE_DATA_TYPE) + enc) if prefix else enc
    try:
        os.write(fd, pkt)
    except Exception as e:
        print("  [%s] write failed: %s" % (label, e)); return
    deadline = time.time() + 2
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                data = os.read(fd, 8192)
            except Exception as e:
                print("  [%s] read err: %s" % (label, e)); return
            if data:
                print("  [%s] %d bytes: %s" % (label, len(data), data[:80].hex()))
                return data
    print("  [%s] no response" % label)

def main():
    print("open %s" % DIAG)
    fd = os.open(DIAG, os.O_RDWR | os.O_NONBLOCK)
    print("switch logging:")
    try_switch_logging(fd)
    print("probe (with 4-byte USER_SPACE prefix):")
    send_and_read(fd, b"\x00", "version", prefix=True)
    send_and_read(fd, b"\x7c", "extbuildid", prefix=True)
    print("probe (raw, no prefix):")
    send_and_read(fd, b"\x00", "version-raw", prefix=False)
    os.close(fd)

if __name__ == "__main__":
    main()
