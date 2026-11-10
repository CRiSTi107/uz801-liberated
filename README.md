# UZ801 — liberating a $10 4G USB-WiFi dongle

Reverse-engineering, security audit, and custom firmware/tooling for the **UZ801 4G
USB-WiFi dongle** (Qualcomm **MSM8916** / Snapdragon 410, Android 4.4.4) — a generic
"UFI/MiFi" stick sold cheaply worldwide. The goal: root it without opening the case,
understand (and neutralize) its backdoors, and replace the stock unauthenticated web
panel with an authenticated "cockpit" running on a real Linux stack on the device.

> ⚠️ **Scope & ethics.** This is security research on **hardware I own**. It documents
> vulnerabilities in a shipping device so owners can understand and harden their own
> units. Nothing here targets anyone else's device or network. Don't use it against
> hardware you don't own. Changing a device's IMEI is illegal in many jurisdictions.

> 🔒 **Privacy & redaction.** This device's real identifiers (IMEI, IMSI, ICCID, MAC,
> SSID) are **masked** (`…XXXXX`) throughout the repo and its history. Passwords/keys
> were never committed (see `.gitignore`).

📄 **New here? Read the [security report (REPORT.md)](REPORT.md)** — the start-to-finish
write-up (root → audit → hardening → outcome).

## What's here

- **A root path with no case-opening** — a hidden `/usbdebug.html` → `funcNo 2001`
  flips the USB gadget to expose ADB; from there the full firmware was dumped
  (25 partitions, md5-verified) over plain `adb pull`.
- **A full security audit** of the dumped firmware — the stock device is *remotely
  rootable*: an unauthenticated, system-privileged `/ajax` API, ADB-root by default,
  unsigned OTA, and a cleartext phone-home to a cloud host that can push firmware.
  See **[docs/security/SECURITY-AUDIT.md](docs/security/SECURITY-AUDIT.md)** and the
  exploit catalog with mitigation status in
  **[docs/security/EXPLOITS.md](docs/security/EXPLOITS.md)**.
- **Hardening** that closes the backdoors while keeping the device working — network
  ADB firewalled off, cellular-WAN inbound dropped, phone-home IP blocked, the stock
  `/ajax` backdoor walled off behind localhost, WiFi key rotated. Lifelines (USB-ADB
  root + SSH) kept.
- **A "cockpit"** — an authenticated (TLS + CSRF) Python-stdlib HTTPS API and SPA that
  runs in an **Alpine/Debian chroot on the dongle itself**, talking to the modem over
  AT (`/dev/smd11`) and QMI, replacing the stock panel. Auto-starts on boot.
- **Modem/RF deep-dives** — SIM diagnosis via root AT, an LTE **band-lock over QMI**
  (the modem has no AT band verb; done via `qmuxd`-adjacent SMD channel), and an honest
  diagnosis of why data still doesn't hold here (see below).

## Hardware at a glance

| | |
|---|---|
| SoC | Qualcomm **MSM8916** (Snapdragon 410, 4×A53 @1.2 GHz) |
| RAM / storage | 512 MB LPDDR3 / 4 GB eMMC |
| WiFi/BT | WCN3620 (2.4 GHz b/g/n) |
| OS | Android 4.4.4 (`userdebug`, test-keys), kernel 3.10 |
| Modem | Gobi-class LTE Cat4; **LTE FDD B1/B3/B5/B8 + TDD B40** |
| USB | gadget-only RNDIS + CDC (appears as a USB Ethernet NIC) |

## The honest outcome

Root, dump, audit, harden, and the on-device cockpit all **work**. Cellular **data/SMS/
voice do not reliably work at the test location** — and it's a **hardware/RF limit, not
software**: the strong local carrier cell is **Band 20 (800 MHz)**, which this dongle
**cannot use**; the bands it *does* support are near-dead here. The band-lock and an
external antenna help the modem register but can't manufacture signal on an unsupported
band. Full reasoning in
[docs/INVESTIGATION-LOG.md](docs/INVESTIGATION-LOG.md) and
[docs/hardware/CAPABILITIES.md](docs/hardware/CAPABILITIES.md).

## Repository layout

```
README.md            ← you are here (overview)
PROGRESS.md          ← canonical current state + "RESUME HERE" next actions
CLAUDE.md            ← working guide for AI-assisted sessions on this repo
docs/                ← all documentation (see docs/README.md for the index)
  security/          ← audit, exploit catalog, phone-home investigation
  hardware/          ← specs, band capabilities, antenna
  plans/             ← cockpit API, chroot, band-lock, Linux-distro options
  INVESTIGATION-LOG.md, SESSION-HISTORY.md
cockpit/             ← authenticated on-device API (api/) + web SPA (web/)
webui/               ← earlier on-device UI + device deploy scripts (harden/boot-hook)
alpine/              ← chroot scaffolding
diag/                ← QMI/DIAG probes + band/antenna test tools
host/                ← PC-side helpers
dump/                ← firmware dump text records (.img blobs gitignored)
```

➡️ **Full documentation index: [docs/README.md](docs/README.md)**

## Reproducing the root (on a unit you own)

```bash
# 1. Enable root ADB — no case opening, reversible
curl -s -d '{"funcNo":2001}' http://192.168.100.1/ajax     # == loading /usbdebug.html
adb wait-for-device && adb shell id                         # expect uid=0(root)

# 2. Dump a partition byte-exact, then verify on-device
adb pull /dev/block/bootdevice/by-name/<name> <name>.img
adb shell "busybox md5sum /dev/block/bootdevice/by-name/<name>"

# 3. Revert to stock USB mode when done
adb shell "setprop persist.sys.usb.config rndis" && adb reboot
```

Device defaults vary by firmware variant; mainboard/firmware variants are **not
cross-flashable**. Keep the per-device calibration partitions (`modemst1/2`, `fsg`,
`fsc`, `persist`) backed up before any flash — they hold the unique IMEI/MAC/NV.
