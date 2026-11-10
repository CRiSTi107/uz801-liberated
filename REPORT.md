# UZ801 4G USB Dongle — Security Report

A start-to-finish security assessment of a **UZ801 4G USB-WiFi dongle** (Qualcomm
MSM8916, Android 4.4.4), a generic "UFI/MiFi" stick sold cheaply worldwide. This report
summarizes how the device was rooted without opening the case, what the firmware audit
found, and how the unit was hardened afterward. Depth lives in [`docs/`](docs/README.md).

- **Scope:** one physical unit I own. No third-party devices or networks were touched.
- **Method:** read-only static analysis of a firmware dump. No extracted binary/APK was
  executed; the dump was inspected with `strings`/`grep`/`jadx`/mount-read-only.
- **Redaction:** this device's real identifiers (IMEI/IMSI/ICCID/MAC/SSID) are masked
  (`…XXXXX`) throughout. No credentials are, or ever were, committed.

---

## 1. Executive summary

Out of the box, this dongle is **remotely rootable by anyone who can reach its LAN**
(WiFi or USB). The stock management API requires **no authentication**, runs with
**system privileges**, and exposes functions that enable ADB-root, rewrite the IMEI,
reboot to bootloader, and flash **unsigned** firmware. The device also **phones home in
cleartext** to a cloud host every ~30 minutes over a channel that can push firmware back.

None of this requires physical access or a software vulnerability in the classic sense —
the "features" are simply exposed without authentication. The findings below are ordered
by severity. Mitigations that were applied to the test unit are noted inline and detailed
in [`docs/security/EXPLOITS.md`](docs/security/EXPLOITS.md).

## 2. Device under test

| | |
|---|---|
| Model | UZ801 (generic 4G UFI/MiFi) |
| SoC | Qualcomm MSM8916 (Snapdragon 410, 4×A53) |
| OS | Android 4.4.4 `userdebug`, **test-keys**, `ro.secure=0`, `ro.debuggable=1` |
| Mgmt UI | Jetty "4G Modem" panel on `http://192.168.100.1/`, default **admin/admin** |
| API | single endpoint `POST /ajax` with a JSON `{"funcNo":N}` body |

## 3. Getting in (no case opening)

The stock USB exposes only RNDIS + CDC-Data and the web UI is "push-only," which *looks*
locked down. But a hidden page **`/usbdebug.html`** issues `POST /ajax {"funcNo":2001}`,
which sets `persist.sys.usb.config` to `rndis,serial_smd,diag,adb` and reboots — the
device re-enumerates with **ADB exposed, as root** (`uid=0`, test-keys build).

From there the full firmware was dumped over plain `adb pull` of each
`/dev/block/bootdevice/by-name/*` partition (25 partitions, every one md5-verified against
the device). Reproduction steps are in the [root README](README.md); the full dump record
is in [`dump/`](dump/) (text records only; the image blobs are not redistributed).

## 4. Findings

### CRITICAL

- **F-1 — `/ajax` has no authentication.** `AjaxSevlet.java` dispatches every `funcNo`
  with no session/cookie check; the "login" (`funcNo 1000`) is cosmetic. Jetty binds
  `0.0.0.0:80` and the app runs as `android.uid.system`. Any WiFi/USB-adjacent host can
  invoke any function with a single POST.
- **F-2 — Unauthenticated root via `funcNo 2001`.** Flips the USB config to include
  `adb` and reboots → root ADB, reachable by anyone on the LAN.
- **F-3 — ADB-root by default.** `ro.secure=0`, `ro.debuggable=1`, and
  `persist.adb.tcp.port=7628` (root ADB over TCP on the LAN).
- **F-4 — Unsigned firmware flash.** An unauthenticated multipart upload writes
  `/data/update.zip`; `funcNo 3000` flashes it with **no signature/md5 verification**.
  Recovery only verifies against the **public AOSP test-key**, so anyone can sign an OTA.

### HIGH

- **F-5 — Cleartext phone-home / remote flash.** Every ~30 min the device GETs
  `http://120.77.62.229:8080/file/checkUpdate?...&imei=<IMEI>` and will download+flash
  whatever the server returns. The md5 "check" is the **server's own** value, so this is a
  **server-driven remote-flash channel**, not just a MITM risk — and it leaks the IMEI in
  cleartext on every poll. It is the **only** external endpoint in the entire image. Deep
  dive: [`docs/security/IP-INVESTIGATION.md`](docs/security/IP-INVESTIGATION.md).
- **F-6 — Arbitrary IMEI rewrite (`funcNo 2002` → `AT+WRIMEI`)**, unauthenticated reboot
  to bootloader (`funcNo 2000`), and unauthenticated disclosure of WiFi PSK / IMSI / ICCID
  / client list / GPS.

### MEDIUM

- **F-7 —** setuid-root `su` and `tcpdump` in `/system/xbin` (`06755`); default creds
  (admin/admin, WiFi `1234567890`); Qualcomm **DIAG** exposed and `/dev/smd11` world-RW
  (`0666`); unauthenticated reboot / factory-reset / DNS-change; root UART console.

**Non-findings:** no web shell-command injection (no `exec(String)`/shell), no extra Unix
accounts, clean `hosts`, benign `cdrom_install.iso`. Full detail and the per-funcNo map
are in [`docs/security/SECURITY-AUDIT.md`](docs/security/SECURITY-AUDIT.md).

## 5. Remediation applied to the test unit

Hardening ([`webui/deploy/harden.sh`](webui/deploy/harden.sh)) that closes the exposure
while keeping the device functional, persisted across reboot via a boot hook:

- Network ADB firewalled off (TCP 7628 dropped); **USB-ADB root kept as a lifeline**.
- New inbound on the cellular WAN (`rmnet0`) dropped.
- Phone-home host `120.77.62.229` blocked (OUTPUT drop).
- The stock unauthenticated `/ajax` backdoor walled off behind localhost; replaced with an
  **authenticated cockpit** (TLS + CSRF, Python-stdlib) running in an on-device
  Alpine/Debian chroot. Design: [`docs/plans/COCKPIT-API-PLAN.md`](docs/plans/COCKPIT-API-PLAN.md).
- Default WiFi key rotated off `1234567890`.

## 6. Note on cellular function (not a vulnerability)

Independently of security, cellular data/SMS/voice do not reliably work at the test
location — a **hardware/RF limit**: the strong local carrier cell is **Band 20 (800 MHz)**,
which this dongle's modem does not support (it has FDD B1/B3/B5/B8 + TDD B40). Included for
completeness; reasoning in [`docs/hardware/CAPABILITIES.md`](docs/hardware/CAPABILITIES.md)
and [`docs/INVESTIGATION-LOG.md`](docs/INVESTIGATION-LOG.md).

## 7. Disclosure & intent

This documents vulnerabilities in a **shipping consumer device**, on a unit I own, so that
other owners can understand and harden their hardware. The affected firmware is a generic
white-label build with no clear single vendor; the phone-home endpoint has been recorded
for awareness. Nothing here is intended to, or should, be used against devices or networks
you do not own. **Changing a device's IMEI is illegal in many jurisdictions.**
