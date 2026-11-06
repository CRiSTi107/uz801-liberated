# Investigation — `120.77.62.229` (UZ801 firmware phone-home / OTA host)

Point-in-time OSINT/analysis. **No active probing of this host was done from the
owner's machine** (to avoid exposing the owner's IP to a possible C2 and tipping
off the operator). The only active scan is the owner's own `nmap` (below). Port
states are a snapshot and can change.

## What this IP is (from firmware analysis)
Hardcoded, **bare IPv4** (no DNS name) compiled into `com.mifiservice.ota.Updater`
in `MifiService.apk`. See `SECURITY-AUDIT.md` (Finding 5) and `EXPLOITS.md` (BD-5):

- Every ~30 min the dongle does cleartext HTTP:
  `GET http://120.77.62.229:8080/file/checkUpdate?&device=<name>&swv=<ver>&imei=<IMEI>`
- It then treats the **response string as the expected md5**, downloads
  `GET http://120.77.62.229:8080/file/doDownload?md5=<that>&imei=<IMEI>`, and flashes
  the result via recovery. No signature, no key pinning — **the server chooses both
  the bytes and the "expected" hash** → arbitrary, persistent, system-level firmware
  push to any device still polling it (deep audit D1).
- It leaks the device **IMEI** in cleartext on every poll.

## Ownership / geolocation
- **Alibaba Cloud (Aliyun)**, China. The `120.77.0.0/16` space is Aliyun
  (East-China / Hangzhou region). (Confirmed as Aliyun in the firmware audit;
  live `whois`/rDNS not available in this environment to re-verify the exact
  netblock/allocation.)
- No PTR/reverse-DNS resolved. The firmware references the **raw IP**, so there is
  no domain to pivot on — and **if Aliyun ever reassigns this address, the new
  tenant inherits push-flash control** of every device still beaconing to it.

## Owner's nmap (2026-11-07, full TCP sweep)
```
PORT      STATE SERVICE
40003/tcp open  unknown
40800/tcp open  unknown
40801/tcp open  unknown
40803/tcp open  unknown
40804/tcp open  unknown
40810/tcp open  unknown
40820/tcp open  unknown
(65485 filtered / no-response, 43 closed / conn-refused)
```

### Analysis
- **Port 8080 — the firmware's OTA port — is NOT in the open list.** It fell into
  the *filtered* (no-response) bucket, not *closed*. That means one of:
  1. it's **firewalled to specific source IPs/subnets** (e.g., only the mobile-carrier
     ranges the dongles appear from) — a scan from a random host sees no response;
  2. the OTA service is **down / moved**; or
  3. **port-knocking / conditional exposure**.
  From the outside this is indistinguishable — but "filtered, not closed" leans
  toward **selective firewalling** rather than the port being dead. The host is
  clearly **up and actively serving** (other ports answer), so the OTA backend may
  well still be live for devices coming from the "right" networks.
- **The `408xx` cluster (40800/40801/40803/40804/40810/40820) + `40003`** are
  unidentified. A tight, deliberately-numbered high-port cluster like this is typical
  of **custom application listeners** — a fleet/management or relay backend rather
  than standard services (no 22/80/443/3306/etc. exposed). Purely structural
  inference; contents unknown (not probed).
- `43 closed (conn-refused)` + `65485 filtered` = a firewalled host that answers on a
  curated set of ports only. Consistent with a managed cloud backend behind a
  security group.

## Risk to this device / owner
- The host is the **single external C2** in the entire firmware (deep audit D6
  scanned all 59 APKs — nothing else phones home). Its capability is **remote
  firmware flash**, which is worst-case (persistent, survives factory-reset-of-data).
- Already **mitigated on the dongle**: `iptables OUTPUT DROP` to `120.77.62.229`
  (`webui/deploy/harden.sh`), and this now **persists across reboot** via the boot
  hook (`/data/webui/boot-hook.sh`, validated 2026-11-07). The dongle can no longer
  reach it regardless of which port the backend uses.

## Recommendations
- **Keep it blocked** (done + persisted). Optionally also blackhole the whole
  `120.77.0.0/16`? No — overbroad (breaks unrelated Aliyun). The single-IP drop is correct.
- If you want deeper intel on those ports, do it from a **throwaway VPS or over Tor**,
  never your home/mobile IP — a live C2 operator can see and correlate scanners.
- Consider reporting the **cleartext, unauthenticated, test-key remote-flash** design
  to the vendor and/or a CERT — it affects every device on this firmware, not just this one.

## Explicitly not done
- No connection to `:8080`, the `408xx` ports, or `/file/checkUpdate` from the owner's
  network. No credential/dir brute force. This file is analysis of the firmware + the
  owner-supplied scan only.
