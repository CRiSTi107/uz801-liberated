"""
uzplatform.py - DIRECT system access for the UZ801 dongle, replacing the stock
MiFi app's unauthenticated ``http://127.0.0.1:80/ajax`` backdoor.

Every feature the cockpit used to proxy to a stock ``funcNo`` is implemented
here by reading the real system instead:

    WiFi status/config   <- /data/misc/wifi/hostapd.conf  (+ restart hostapd)
    WiFi clients         <- /data/misc/dhcp/dnsmasq.leases + /proc/net/arp
    MAC filter           <- hostapd macaddr_acl + accept/deny mac files
    Traffic counters     <- /proc/net/dev  (rmnet0 WAN, br0 LAN)
    Interface IPs        <- SIOCGIFADDR ioctl (rmnet0 / br0)
    DNS servers          <- resolv.conf / dnsmasq upstreams  (+ modem AT)
    Device info          <- getprop  (ro.product.model, ro.build.* ...)

It is deliberately defensive: every reader returns ``None`` / ``{}`` / ``[]``
on any failure (missing file, parse error, permission) so the caller can fall
back to the stock proxy when ``prefer_direct`` is set but direct access is not
yet possible. Nothing here raises for an absent file.

The module is NAMED ``uzplatform`` (not ``platform``) on purpose: a top-level
``platform.py`` would shadow the Python standard-library ``platform`` module
for the whole process.

Stdlib only (os, re, socket, struct, fcntl, subprocess, signal, time).

Device facts this module encodes (UZ801, Android 4.4.4, kernel 3.10, busybox):
  * The cockpit runs in an Alpine armv7 chroot sharing the host net namespace
    with host /proc, /sys, /dev bind-mounted, and host tools reachable by
    absolute path under /system/bin.
  * hostapd runs as:  /system/bin/hostapd -e <entropy.bin> <hostapd.conf>
    (a child of netd). Config is standard hostapd.conf key=value.
  * dnsmasq (child of netd) writes leases to /data/misc/dhcp/dnsmasq.leases.
  * br0 = 192.168.100.1/24 over wlan0 + rndis0 (built by initmifiservice.sh).
  * Cellular WAN = rmnet0.
"""

import os
import re
import signal
import socket
import struct
import subprocess
import time

from logutil import get

log = get("cockpit.platform")

# ioctl request code for "get interface IPv4 address" (Linux, arch-independent
# for the classic ifreq ioctls). Pure stdlib via fcntl + struct.
_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B

DEFAULTS = {
    "prefer_direct": True,
    "wan_iface": "rmnet0",
    "lan_iface": "br0",
    "hostapd_conf": "/data/misc/wifi/hostapd.conf",
    "hostapd_bin": "/system/bin/hostapd",
    "hostapd_entropy": "/data/misc/wifi/entropy.bin",
    "dnsmasq_leases": "/data/misc/dhcp/dnsmasq.leases",
    "proc_net_dev": "/proc/net/dev",
    "proc_net_arp": "/proc/net/arp",
    "getprop_bin": "/system/bin/getprop",
    # resolv.conf candidates (first that exists / has nameservers wins). The
    # chroot's own /etc/resolv.conf and the host path are both tried.
    "resolv_conf": ["/etc/resolv.conf", "/data/misc/dhcp/resolv.conf"],
    # Accept/deny MAC list files when the hostapd.conf does not name them.
    "accept_mac_file": "/data/misc/wifi/hostapd.accept",
    "deny_mac_file": "/data/misc/wifi/hostapd.deny",
    # getprop keys surfaced as device info.
    "device_props": [
        "ro.product.model", "ro.product.manufacturer", "ro.product.name",
        "ro.product.device", "ro.build.version.release",
        "ro.build.version.sdk", "ro.build.display.id", "ro.build.fingerprint",
        "ro.serialno",
    ],
}


def _merge_defaults(cfg):
    out = dict(DEFAULTS)
    for k, v in (cfg or {}).items():
        out[k] = v
    return out


# ===========================================================================
# WiFi encryption mapping (API <-> hostapd.conf)
# ===========================================================================
# The SPA/contract encryption values: open | wpa2 | wpa2wpa3 | wpa3.
# This KitKat-era hostapd has NO WPA3/SAE support, so wpa3 and the mixed mode
# both degrade to WPA2-CCMP with a warning surfaced to the client (matching the
# behaviour the stock-proxy path already had in server.py).
def api_to_hostapd(encryption):
    """Return (updates:dict, note:str|None) of hostapd keys for an API value.

    A value of ``None`` in ``updates`` means the key should be *removed* from
    hostapd.conf. ``wpa_passphrase`` is handled by the caller (needs the pw).
    """
    enc = (encryption or "").lower()
    if enc == "open":
        return ({
            "wpa": None, "wpa_key_mgmt": None, "wpa_pairwise": None,
            "rsn_pairwise": None, "wpa_passphrase": None,
        }, None)
    if enc == "wpa2":
        return ({
            "wpa": "2", "wpa_key_mgmt": "WPA-PSK",
            "wpa_pairwise": "CCMP", "rsn_pairwise": "CCMP",
        }, None)
    if enc == "wpa2wpa3":
        return ({
            "wpa": "2", "wpa_key_mgmt": "WPA-PSK",
            "wpa_pairwise": "CCMP", "rsn_pairwise": "CCMP",
        }, "WPA2/WPA3 mixed mode is not supported by this device's hostapd; "
           "saved as WPA2-CCMP.")
    if enc == "wpa3":
        return ({
            "wpa": "2", "wpa_key_mgmt": "WPA-PSK",
            "wpa_pairwise": "CCMP", "rsn_pairwise": "CCMP",
        }, "WPA3 is not supported by this device's hostapd; saved as the best "
           "available WPA2-CCMP.")
    # Unknown -> safest secured default.
    return ({
        "wpa": "2", "wpa_key_mgmt": "WPA-PSK",
        "wpa_pairwise": "CCMP", "rsn_pairwise": "CCMP",
    }, None)


def hostapd_to_api_encryption(conf):
    """Map a parsed hostapd.conf dict back to an API encryption value."""
    wpa = (conf.get("wpa") or "").strip()
    if not wpa or wpa == "0":
        return "open"
    # 1 = WPA, 2 = WPA2, 3 = WPA+WPA2. The closest API value is wpa2.
    return "wpa2"


# ===========================================================================
# hostapd.conf parse / rewrite (order-preserving)
# ===========================================================================
_CONF_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$")


def parse_conf(text):
    """Parse ``key=value`` config text into an ordered dict (last wins).

    Comments (``#``) and blank lines are ignored for the value map.
    """
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = _CONF_LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def rewrite_conf(text, updates):
    """Return new config text with ``updates`` applied, order preserved.

    For each key in ``updates``: a non-None value replaces the first existing
    occurrence (and drops any later duplicates) or is appended if absent; a
    ``None`` value removes every occurrence of that key. Comments and unrelated
    lines are left untouched.
    """
    lines = text.splitlines()
    seen = set()
    result = []
    for line in lines:
        m = _CONF_LINE.match(line)
        key = m.group(1) if m else None
        if key is not None and key in updates:
            val = updates[key]
            if val is None:
                continue                      # delete this line
            if key in seen:
                continue                      # drop duplicate
            result.append("%s=%s" % (key, val))
            seen.add(key)
        else:
            result.append(line)
    # Append any keys that did not already exist.
    for key, val in updates.items():
        if val is None or key in seen:
            continue
        result.append("%s=%s" % (key, val))
    # Preserve a trailing newline.
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(result) + ("\n" if result else "") + ("" if result else trailing)


# ===========================================================================
# Low-level /proc + ioctl readers (module-level, reused by Platform)
# ===========================================================================
def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def iface_ipv4(name):
    """IPv4 address of an interface via SIOCGIFADDR, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl_ioctl(s, _SIOCGIFADDR, name)
        if packed is None:
            return None
        return socket.inet_ntoa(packed[20:24])
    finally:
        s.close()


def iface_netmask(name):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl_ioctl(s, _SIOCGIFNETMASK, name)
        if packed is None:
            return None
        return socket.inet_ntoa(packed[20:24])
    finally:
        s.close()


def fcntl_ioctl(sock, request, iface):
    """Run an ifreq ioctl; returns the 256-byte result or None on failure.

    Imported lazily so the module still imports on a platform without fcntl
    (e.g. a non-Linux dev box running --mock).
    """
    try:
        import fcntl
    except ImportError:
        return None
    try:
        ifr = struct.pack("256s", iface[:15].encode("ascii", "replace"))
        return fcntl.ioctl(sock.fileno(), request, ifr)
    except OSError:
        return None


def parse_proc_net_dev(text, iface):
    """Return (rx_bytes, tx_bytes) for ``iface`` from /proc/net/dev text."""
    if not text:
        return (None, None)
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        if name.strip() != iface:
            continue
        fields = rest.split()
        # receive:  bytes packets errs drop fifo frame compressed multicast
        # transmit: bytes packets errs drop fifo colls carrier compressed
        try:
            rx = int(fields[0])
            tx = int(fields[8])
            return (rx, tx)
        except (IndexError, ValueError):
            return (None, None)
    return (None, None)


def parse_arp(text, iface=None):
    """Parse /proc/net/arp into [{ip, mac, device}] (complete entries only)."""
    out = []
    if not text:
        return out
    lines = text.splitlines()
    for line in lines[1:]:                     # skip header
        parts = line.split()
        if len(parts) < 6:
            continue
        ip, _hwtype, flags, mac, _mask, dev = parts[:6]
        if flags == "0x0" or mac == "00:00:00:00:00:00":
            continue                           # incomplete entry
        if iface and dev != iface:
            continue
        out.append({"ip": ip, "mac": mac.lower(), "device": dev})
    return out


def parse_leases(text):
    """Parse dnsmasq.leases: ``<expiry> <mac> <ip> <hostname> <clientid>``."""
    out = []
    if not text:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        expiry, mac, ip = parts[0], parts[1].lower(), parts[2]
        hostname = parts[3] if len(parts) > 3 and parts[3] != "*" else None
        try:
            exp = int(expiry)
        except ValueError:
            exp = None
        out.append({"mac": mac, "ip": ip, "hostname": hostname,
                    "expires": exp})
    return out


def parse_resolv(text):
    out = []
    if not text:
        return out
    for line in text.splitlines():
        m = re.match(r"\s*nameserver\s+(\S+)", line)
        if m:
            out.append(m.group(1))
    return out


def running_argv(match):
    """Find a running process whose argv[0] basename contains ``match``.

    Returns (pid:int, argv:list[str]) or (None, None). Scans /proc/*/cmdline.
    """
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return (None, None)
    for pid in pids:
        raw = read_bytes("/proc/%s/cmdline" % pid)
        if not raw:
            continue
        argv = [a for a in raw.split(b"\x00") if a]
        if not argv:
            continue
        exe = os.path.basename(argv[0].decode("utf-8", "replace"))
        if match in exe:
            return (int(pid), [a.decode("utf-8", "replace") for a in argv])
    return (None, None)


def read_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


# ===========================================================================
# Platform - the direct control surface
# ===========================================================================
class Platform:
    def __init__(self, cfg):
        self.cfg = _merge_defaults(cfg)

    # --- traffic / addresses --------------------------------------------------
    def traffic(self):
        """Return {wan:{rx_bytes,tx_bytes}, lan:{...}} from /proc/net/dev.

        ``rx`` is download (bytes received from the network), ``tx`` is upload.
        """
        text = read_text(self.cfg["proc_net_dev"])
        out = {}
        for role, iface in (("wan", self.cfg["wan_iface"]),
                            ("lan", self.cfg["lan_iface"])):
            rx, tx = parse_proc_net_dev(text, iface)
            out[role] = {"iface": iface, "rx_bytes": rx, "tx_bytes": tx}
        return out

    def addresses(self):
        """Return {wan:{ip,netmask}, lan:{ip,netmask}} via SIOCGIFADDR."""
        out = {}
        for role, iface in (("wan", self.cfg["wan_iface"]),
                            ("lan", self.cfg["lan_iface"])):
            out[role] = {"iface": iface, "ip": iface_ipv4(iface),
                         "netmask": iface_netmask(iface)}
        return out

    def dns_servers(self):
        """DNS servers from the first resolv.conf candidate with nameservers."""
        candidates = self.cfg["resolv_conf"]
        if isinstance(candidates, str):
            candidates = [candidates]
        for path in candidates:
            servers = parse_resolv(read_text(path))
            if servers:
                return servers
        return []

    # --- WiFi -----------------------------------------------------------------
    def _read_hostapd(self):
        text = read_text(self.cfg["hostapd_conf"])
        if text is None:
            return (None, None)
        return (text, parse_conf(text))

    def wifi_get(self):
        """Return {ssid, password, encryption, max_clients, channel} or None."""
        text, conf = self._read_hostapd()
        if conf is None:
            return None
        return {
            "ssid": conf.get("ssid", ""),
            "password": conf.get("wpa_passphrase", ""),
            "encryption": hostapd_to_api_encryption(conf),
            "max_clients": _to_int(conf.get("max_num_sta")),
            "channel": _to_int(conf.get("channel")),
        }

    def wifi_set(self, ssid=None, password=None, encryption=None,
                 max_clients=None, channel=None):
        """Rewrite hostapd.conf for the given fields and restart hostapd.

        Returns {ok, warn, restart, encryption}. Raises ValueError on an
        invalid WPA passphrase (hostapd requires 8..63 chars).
        """
        text, conf = self._read_hostapd()
        if text is None:
            raise RuntimeError("hostapd.conf not readable: %s"
                               % self.cfg["hostapd_conf"])
        updates = {}
        note = None
        if ssid is not None:
            updates["ssid"] = str(ssid)
        if channel is not None:
            updates["channel"] = str(_to_int(channel) or conf.get("channel"))
        if max_clients is not None:
            mc = _to_int(max_clients)
            if mc is not None:
                updates["max_num_sta"] = str(mc)
        if encryption is not None:
            enc_updates, note = api_to_hostapd(encryption)
            updates.update(enc_updates)
            # Only secured modes carry a passphrase.
            if enc_updates.get("wpa") in (None,):
                updates["wpa_passphrase"] = None
        if password is not None and (encryption is None
                                     or (encryption or "").lower() != "open"):
            pw = str(password)
            if not (8 <= len(pw) <= 63):
                raise ValueError(
                    "WPA passphrase must be 8..63 characters")
            updates["wpa_passphrase"] = pw

        new_text = rewrite_conf(text, updates)
        self._atomic_write(self.cfg["hostapd_conf"], new_text)
        restart = self.restart_hostapd()
        out = {"ok": True, "restart": restart,
               "encryption": hostapd_to_api_encryption(parse_conf(new_text))}
        if note:
            out["warn"] = note
        return out

    def _atomic_write(self, path, text):
        tmp = path + ".cockpit.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def restart_hostapd(self):
        """Kill the running hostapd and relaunch it with the same argv.

        Returns {ok, pid, relaunched, argv, note}. If no hostapd is running we
        launch a fresh one with ``hostapd_bin -e entropy.bin hostapd.conf``.
        netd may also respawn hostapd on its own; relaunch is best-effort.
        """
        pid, argv = running_argv("hostapd")
        killed = False
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
                # Give it a moment, then SIGKILL if still alive.
                for _ in range(20):
                    time.sleep(0.05)
                    if not _pid_alive(pid):
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError as exc:
                log.event(30, "hostapd.kill_failed", pid=pid, err=str(exc))
        if not argv:
            argv = [self.cfg["hostapd_bin"], "-e", self.cfg["hostapd_entropy"],
                    self.cfg["hostapd_conf"]]
        relaunched = False
        try:
            # Detach so hostapd outlives the request; discard its stdio.
            with open(os.devnull, "wb") as dn:
                subprocess.Popen(argv, stdin=dn, stdout=dn, stderr=dn,
                                 close_fds=True, start_new_session=True)
            relaunched = True
        except OSError as exc:
            log.event(40, "hostapd.relaunch_failed", err=str(exc))
        return {"ok": relaunched, "pid": pid, "killed": killed,
                "relaunched": relaunched, "argv": argv,
                "note": "netd may also respawn hostapd"}

    # --- WiFi clients ---------------------------------------------------------
    def wifi_clients(self):
        """Return [{hostname, ip, mac}] on the LAN from leases + ARP."""
        leases = parse_leases(read_text(self.cfg["dnsmasq_leases"]))
        arp = parse_arp(read_text(self.cfg["proc_net_arp"]),
                        iface=self.cfg["lan_iface"])
        by_mac = {}
        for l in leases:
            by_mac[l["mac"]] = {"hostname": l["hostname"], "ip": l["ip"],
                                "mac": l["mac"]}
        # ARP shows who is actually present now; add/merge entries.
        for a in arp:
            entry = by_mac.get(a["mac"])
            if entry:
                entry.setdefault("ip", a["ip"])
                if not entry.get("ip"):
                    entry["ip"] = a["ip"]
            else:
                by_mac[a["mac"]] = {"hostname": None, "ip": a["ip"],
                                    "mac": a["mac"]}
        return list(by_mac.values())

    # --- MAC filter -----------------------------------------------------------
    def _acl_paths(self, conf):
        accept = conf.get("accept_mac_file") or self.cfg["accept_mac_file"]
        deny = conf.get("deny_mac_file") or self.cfg["deny_mac_file"]
        return accept, deny

    def macfilter_get(self):
        """Return {mode, accept, deny} from hostapd macaddr_acl + mac files.

        mode: 'disabled' (acl 0, no deny list), 'deny' (acl 0 w/ deny list =
        blacklist) or 'accept' (acl 1 = whitelist).
        """
        text, conf = self._read_hostapd()
        if conf is None:
            return None
        acl = (conf.get("macaddr_acl") or "0").strip()
        accept_path, deny_path = self._acl_paths(conf)
        accept = _read_mac_file(accept_path)
        deny = _read_mac_file(deny_path)
        if acl == "1":
            mode = "accept"                     # whitelist
        elif deny:
            mode = "deny"                        # blacklist in use
        else:
            mode = "disabled"
        return {"mode": mode, "macaddr_acl": acl,
                "accept": accept, "deny": deny}

    def macfilter_set(self, mode=None, accept=None, deny=None):
        """Set MAC filter mode / lists and restart hostapd.

        mode: 'disabled' | 'accept' (whitelist) | 'deny' (blacklist).
        accept/deny: lists of MAC strings (optional; replace the file).
        """
        text, conf = self._read_hostapd()
        if text is None:
            raise RuntimeError("hostapd.conf not readable")
        accept_path, deny_path = self._acl_paths(conf)
        updates = {"accept_mac_file": accept_path, "deny_mac_file": deny_path}
        if accept is not None:
            _write_mac_file(accept_path, accept)
        if deny is not None:
            _write_mac_file(deny_path, deny)
        if mode == "accept":
            updates["macaddr_acl"] = "1"
        elif mode == "deny":
            updates["macaddr_acl"] = "0"
        elif mode == "disabled":
            updates["macaddr_acl"] = "0"
        new_text = rewrite_conf(text, updates)
        self._atomic_write(self.cfg["hostapd_conf"], new_text)
        restart = self.restart_hostapd()
        return {"ok": True, "mode": mode, "restart": restart}

    # --- device info ----------------------------------------------------------
    def getprop(self, name):
        """Read one Android system property via /system/bin/getprop."""
        binp = self.cfg["getprop_bin"]
        try:
            proc = subprocess.run([binp, name], capture_output=True,
                                  text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        val = (proc.stdout or "").strip()
        return val or None

    def device_info(self):
        """Return a dict of selected getprop values (model, build, ...)."""
        out = {}
        for key in self.cfg["device_props"]:
            val = self.getprop(key)
            if val is not None:
                out[key] = val
        return out


# ===========================================================================
# Mock platform - canned values so --mock and tests run with no device
# ===========================================================================
class MockPlatform(Platform):
    def __init__(self, cfg):
        super().__init__(cfg)
        self._wifi = {
            "ssid": "UZ801-mock",
            "password": "mockpass123",
            "encryption": "wpa2",
            "max_clients": 8,
            "channel": 6,
        }
        self._macfilter = {"mode": "disabled", "macaddr_acl": "0",
                           "accept": [], "deny": []}
        self._rx = 20971520
        self._tx = 1048576

    def traffic(self):
        # Slowly grow so the UI counters look live.
        self._rx += 4096
        self._tx += 1024
        return {
            "wan": {"iface": self.cfg["wan_iface"], "rx_bytes": self._rx,
                    "tx_bytes": self._tx},
            "lan": {"iface": self.cfg["lan_iface"], "rx_bytes": self._rx * 2,
                    "tx_bytes": self._tx * 2},
        }

    def addresses(self):
        return {
            "wan": {"iface": self.cfg["wan_iface"], "ip": "10.74.88.195",
                    "netmask": "255.255.255.0"},
            "lan": {"iface": self.cfg["lan_iface"], "ip": "192.168.100.1",
                    "netmask": "255.255.255.0"},
        }

    def dns_servers(self):
        return ["8.8.8.8", "1.1.1.1"]

    def wifi_get(self):
        return dict(self._wifi)

    def wifi_set(self, ssid=None, password=None, encryption=None,
                 max_clients=None, channel=None):
        note = None
        if ssid is not None:
            self._wifi["ssid"] = str(ssid)
        if password is not None:
            self._wifi["password"] = str(password)
        if encryption is not None:
            _, note = api_to_hostapd(encryption)
            enc = (encryption or "").lower()
            self._wifi["encryption"] = "open" if enc == "open" else "wpa2"
        if max_clients is not None:
            self._wifi["max_clients"] = _to_int(max_clients)
        if channel is not None:
            self._wifi["channel"] = _to_int(channel)
        out = {"ok": True, "encryption": self._wifi["encryption"],
               "restart": {"ok": True, "pid": 1234, "relaunched": True,
                           "argv": ["hostapd"], "mock": True}}
        if note:
            out["warn"] = note
        return out

    def wifi_clients(self):
        return [
            {"hostname": "phone", "ip": "192.168.100.101",
             "mac": "aa:bb:cc:dd:ee:01"},
            {"hostname": "laptop", "ip": "192.168.100.102",
             "mac": "aa:bb:cc:dd:ee:02"},
        ]

    def macfilter_get(self):
        return dict(self._macfilter)

    def macfilter_set(self, mode=None, accept=None, deny=None):
        if mode is not None:
            self._macfilter["mode"] = mode
            self._macfilter["macaddr_acl"] = "1" if mode == "accept" else "0"
        if accept is not None:
            self._macfilter["accept"] = list(accept)
        if deny is not None:
            self._macfilter["deny"] = list(deny)
        return {"ok": True, "mode": mode,
                "restart": {"ok": True, "mock": True}}

    def device_info(self):
        return {
            "ro.product.model": "UZ801",
            "ro.product.manufacturer": "Qualcomm",
            "ro.build.version.release": "4.4.4",
            "ro.build.version.sdk": "19",
            "ro.build.display.id": "UZ801 V2.3.12 (mock)",
        }

    def getprop(self, name):
        return self.device_info().get(name)


# ===========================================================================
# Helpers + factory
# ===========================================================================
def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_mac_file(path):
    text = read_text(path)
    if not text:
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # hostapd accept/deny files are one MAC per line (optional VLAN after).
        mac = s.split()[0].lower()
        if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac):
            out.append(mac)
    return out


def _write_mac_file(path, macs):
    norm = []
    for m in macs or []:
        mac = str(m).strip().lower()
        if re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac):
            norm.append(mac)
    body = "\n".join(norm) + ("\n" if norm else "")
    tmp = path + ".cockpit.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def build_platform(cfg, mock=False):
    """Create a Platform (or MockPlatform) from the ``platform`` config dict."""
    if mock:
        log.event(20, "platform.mock_enabled")
        return MockPlatform(cfg or {})
    return Platform(cfg or {})
