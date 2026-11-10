# Installing Linux on the UZ801 — Viability Assessment

Assessment only — **nothing was changed on the device.** See [`SPECS.md`](../hardware/SPECS.md)
for the hardware this is based on.

## Framing
The dongle **already runs Linux**: Android 4.4.4 on kernel **3.10.28, ARMv7 (32-bit)**.
So "install Linux" means one of two very different projects:

- **A** — a real GNU/Linux *userland on top of* Android (chroot/proot) — easy.
- **B** — *replace* Android with native mainline Linux (postmarketOS-style) — hard.

---

## Option A — GNU/Linux userland on top of Android (chroot / proot)
**Verdict: fully viable, low-risk, ~1 hour, reversible.**

- We have root; drop an **Alpine (~50 MB)** or **Debian armhf (~300 MB)** rootfs into
  `/data` (≈700 MB free) and enter via `chroot` / `proot`.
- Gives a genuine Linux box: real `apk`/`apt`, normal filesystem, SSH, services.
- The Android modem stack keeps running underneath, so the dongle **stays a working
  4G modem**, managed by the web UI at `http://192.168.100.1:8088/`.

**Caveats**
- Shares Android's old **3.10 kernel** (no modern kernel features).
- **32-bit ARMv7** userland.
- **Headless** — CLI/server only (no display on this device).
- RAM tight: **~388 MB usable** + 192 MB zram.

**Typical use:** on-dongle router / VPN / tooling / scripting box.
**Recommended** if the goal is simply "a usable Linux environment on the dongle."

---

## Option B — Replace Android with native mainline Linux (postmarketOS)
**Verdict: technically possible but a real porting project — weeks, with brick risk.
Not a plug-in install.**

**In our favor**
- **MSM8916 is one of the best-mainlined Qualcomm SoCs** (postmarketOS / mainline run
  on many msm8916 phones).
- A53 cores are **64-bit capable** → could run aarch64 (device is currently 32-bit).
- **EDL (9008) access** exists (funcNo `2000`), build is **eng / test-keys**, and we
  have **full partition dumps** (`dump/`) → recovery is plausible if a flash fails.

**Hard blockers**
1. **No existing UZ801 port.** Mainline has generic msm8916 support, but *this* board
   (a screenless UFI dongle: USB-gadget networking, WCN3620 WiFi, the modem) needs a
   device port — a DTB and config for its specific peripherals. Nobody has done it.
2. **Modem bring-up is the worst part.** Keeping it a 4G modem under mainline needs
   `qrtr` + `rmtfs` + modem firmware (we have `modem.img`) + ModemManager/oFono —
   doable on msm8916 but fiddly, and it's the entire point of the device.
3. **Bootloader state is the one unknown.** EDL works, but a clean path needs
   fastboot-unlock (or a firehose/MBN for EDL flashing). **Verify this before
   committing** (it reboots the device, so it wasn't poked during this assessment).

**Risk:** replacing Android can brick the device; recoverable only via EDL with the
right programmer. Much higher stakes than Option A.

---

## Constraints at a glance
| | |
|---|---|
| CPU | MSM8916, 2× Cortex-A53 online @ ≤1.19 GHz (ARMv8/64-bit capable; currently 32-bit) |
| RAM | 512 MB (~388 MB usable) + 192 MB zram — minimal distros only |
| Storage | ~4 GB eMMC (system 775 M / data 775 M) — fine for minimal Linux |
| Display | none — headless CLI/server Linux only |
| Modem / WiFi | need proprietary blobs (we have `modem.img`; WCN3620 nv in `persist`) |
| Bootloader | EDL (9008) reachable; fastboot-unlock state **unconfirmed** |

---

## Bottom line
- Want a usable Linux environment on the dongle → **Option A** is the viable
  "just install Linux" plan (easy, safe, keeps the modem).
- Want native mainline Linux → **Option B** is feasible given MSM8916's good mainline
  support + EDL access + our dumps, **but** it's a multi-week device port whose success
  hinges on modem bring-up and confirming the bootloader is unlockable — not a quick install.

**Pre-flight for Option B (when/if pursued):** confirm fastboot-unlock / EDL firehose
path; collect the board DTS peripherals; plan modem (`qrtr`/`rmtfs`) + WiFi firmware;
keep `dump/` as the EDL restore image set.
