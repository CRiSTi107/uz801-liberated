#!/usr/bin/env python3
"""EFS2-over-DIAG reader for the UZ801 modem (READ-ONLY).
Builds on the proven /dev/diag transport (see qcdiag_probe.py) and implements
the minimum EFS2 ops (HELLO, OPEN, READ, CLOSE) to READ modem NV/EFS item files.
Tries the common LTE band-config paths and prints their raw bytes. Writes nothing.

Run in the Alpine chroot as root (with /dev/diag bind-mounted):
    /system/bin/sh /data/linux/enter.sh 'python3 /opt/diag/qcdiag_efs.py'
"""
import os, fcntl, struct, select, time

DIAG = "/dev/diag"
DIAG_IOCTL_SWITCH_LOGGING = 7
MEMORY_DEVICE_MODE = 2
USER_SPACE_DATA_TYPE = 0x00000020
SUBSYS_CMD = 0x4B
EFS2_SUBSYS = 0x13
# EFS2 subsys command codes
EFS_HELLO, EFS_OPEN, EFS_CLOSE, EFS_READ = 0x00, 0x02, 0x03, 0x04

def crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if (c & 1) else (c >> 1)
    return c ^ 0xFFFF

def hdlc_encode(p):
    f = bytes(p) + struct.pack('<H', crc16(p))
    o = bytearray()
    for b in f:
        o += bytes([0x7D, b ^ 0x20]) if b in (0x7E, 0x7D) else bytes([b])
    o.append(0x7E)
    return bytes(o)

def hdlc_decode(d):
    if d and d[-1] == 0x7E:
        d = d[:-1]
    o = bytearray(); esc = False
    for b in d:
        if esc: o.append(b ^ 0x20); esc = False
        elif b == 0x7D: esc = True
        else: o.append(b)
    return bytes(o[:-2]) if len(o) >= 2 else b""   # drop FCS

class Diag:
    def __init__(self):
        self.fd = os.open(DIAG, os.O_RDWR | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, DIAG_IOCTL_SWITCH_LOGGING,
                    struct.pack('<iiii', MEMORY_DEVICE_MODE, 0, -1, 0))

    def _read_frame(self, timeout=2.0):
        buf = bytearray(); end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.3)
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 16384)
            except OSError:
                time.sleep(0.02); continue
            if not chunk:
                continue
            buf += chunk
            if 0x7E in buf:
                # driver header is 4*N bytes before the HDLC frame; find frame
                fend = buf.index(0x7E)
                # locate frame start: skip the 12-byte (type,count,len) header if present
                start = 12 if len(buf) > 12 else 0
                frame = bytes(buf[start:fend + 1])
                return hdlc_decode(frame), bytes(buf)
        return None, bytes(buf)

    def _drain(self):
        while True:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                return
            try:
                if not os.read(self.fd, 16384):
                    return
            except OSError:
                return

    def _write(self, data):
        for _ in range(8):
            try:
                os.write(self.fd, data); return True
            except OSError as e:
                time.sleep(0.05)
        return False

    def subsys(self, cmd_code, payload=b""):
        self._drain()
        pkt = bytes([SUBSYS_CMD, EFS2_SUBSYS]) + struct.pack('<H', cmd_code) + payload
        self._write(struct.pack('<I', USER_SPACE_DATA_TYPE) + hdlc_encode(pkt))
        return self._read_frame()

    def hello(self):
        # window/bytecnt/version negotiation
        p = struct.pack('<IIIIIIII', 0x100, 0x400, 0x100, 0x400, 1, 1, 1, 0)
        resp, raw = self.subsys(EFS_HELLO, p)
        return resp, raw

    @staticmethod
    def _payload(resp, cmd_code):
        """Return the bytes after the '4b 13 <cmd:LE16>' marker, or None."""
        if not resp:
            return None
        marker = bytes([SUBSYS_CMD, EFS2_SUBSYS]) + struct.pack('<H', cmd_code)
        i = resp.find(marker)
        return resp[i + len(marker):] if i >= 0 else None

    def open(self, path):
        p = struct.pack('<II', 0, 0) + path.encode() + b'\x00'  # oflag=O_RDONLY
        resp, raw = self.subsys(EFS_OPEN, p)
        body = self._payload(resp, EFS_OPEN)
        if body is None or len(body) < 8:
            return None, -999, raw
        fd, errno = struct.unpack('<ii', body[:8])
        return fd, errno, raw

    def read(self, fd, nbytes=512, offset=0):
        p = struct.pack('<iII', fd, offset, nbytes)
        resp, _ = self.subsys(EFS_READ, p)
        body = self._payload(resp, EFS_READ)
        if body is None or len(body) < 16:
            return b"", -999
        rfd, roff, nread, errno = struct.unpack('<iiii', body[:16])
        data = body[16:16 + nread] if nread > 0 else b""
        return data, errno

    def close(self, fd):
        self.subsys(EFS_CLOSE, struct.pack('<i', fd))

    def raw_cmd(self, payload):
        """Send a top-level (non-subsys) DIAG command, return decoded frame."""
        self._drain()
        self._write(struct.pack('<I', USER_SPACE_DATA_TYPE) + hdlc_encode(payload))
        return self._read_frame()

d = Diag()
print("EFS2 HELLO:")
resp, raw = d.hello()
print("  decoded:", (resp[:40].hex() if resp else None))
print("  raw    :", raw[:48].hex())

# Probe which EFS2 subcommands respond (diagnostic)
# Correct EFS2 subcmd numbers: QUERY=1 OPEN=2 READ=4 OPENDIR=11 READDIR=12
#   CLOSEDIR=13 STAT=15 LSTAT=16 STATFS=19
EFS_QUERY, EFS_STAT, EFS_LSTAT, EFS_STATFS = 0x01, 0x0F, 0x10, 0x13
print("probe EFS2 subcommands (raw dumps):")
for name, code, pl in [
    ("QUERY", EFS_QUERY, b""),
    ("STATFS /", EFS_STATFS, b"/\x00"),
    ("STAT /nv", EFS_STAT, b"/nv\x00"),
    ("STAT lte_bc", EFS_STAT, b"/nv/item_files/modem/mmode/lte_bc_config\x00"),
    ("OPEN lte_bc", EFS_OPEN, struct.pack('<II', 0, 0) + b"/nv/item_files/modem/mmode/lte_bc_config\x00"),
]:
    r, rw = d.subsys(code, pl)
    print("  %-14s decoded=%s" % (name, (r.hex() if r else None)))

CANDIDATES = [
    "/nv/item_files/modem/mmode/lte_bandpref",
    "/nv/item_files/modem/mmode/sw_lte_bc_config_bitmask",
    "/nv/item_files/modem/mmode/lte_bc_config",
    "/nv/item_files/rfnv/00065633",
    "/nv/item_files/modem/lte/rrc/cap/band_prior_list",
    "/nv/item_files/modem/mmode/band_pref",
]
# EFS2 file I/O is silently dropped (commercial diag build). Probe the LEGACY
# numbered-NV interface instead (DIAG_NV_READ_F 0x26) — read-only.
print("legacy NV_READ probe (read-only, 0x26):")
DIAG_NV_READ_F = 0x26
# Request: cmd(0x26) + item(uint16 LE) + 128 bytes NV data area (zeros for read)
# Response: cmd(0x26) + item(2) + 128 data + status(2)
for item in (10, 71, 85, 441, 946, 2954):  # benign/mixed items incl. band-pref
    req = bytes([DIAG_NV_READ_F]) + struct.pack('<H', item) + (b'\x00' * 128)
    r, _ = d.raw_cmd(req)
    if r and len(r) >= 3 and r[0] == DIAG_NV_READ_F:
        ritem, = struct.unpack('<H', r[1:3])
        status = struct.unpack('<H', r[131:133])[0] if len(r) >= 133 else -1
        data = r[3:131]
        print("  NV %-5d -> item=%d status=%d data=%s"
              % (item, ritem, status, data[:24].hex()))
    else:
        print("  NV %-5d -> %s" % (item, (r.hex() if r else None)))

print("try candidate band files (read-only):")
for path in CANDIDATES:
    fd, errno, raw = d.open(path)
    if fd is not None and fd >= 0 and errno == 0:
        data, rerr = d.read(fd, 256)
        print("  OPEN %-55s fd=%d -> READ errno=%d len=%d\n     bytes=%s"
              % (path, fd, rerr, len(data), data.hex()))
        d.close(fd)
    else:
        print("  OPEN %-55s errno=%s raw=%s" % (path, errno, raw[:40].hex() if raw else None))
os.close(d.fd)
