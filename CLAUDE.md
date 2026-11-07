# CLAUDE.md — repo guide for Claude Code (read this first)

This repo is a reverse-engineering / security / custom-firmware project on a **UZ801
4G USB-WiFi dongle** (Qualcomm MSM8916, Android 4.4.4). If you're a fresh session (or a
new machine that pulled this repo), **start here, then read [`PROGRESS.md`](PROGRESS.md)**
— it is the canonical, always-current state + "resume here" next actions.
Day-by-day history is the Status Log in [`README.md`](README.md).

## Standing rules for this repository (always do these)
1. **Keep continuity portable.** At the end of any working session, UPDATE:
   - [`PROGRESS.md`](PROGRESS.md) — the status table, the on-device architecture, and the
     top **"▶ RESUME HERE"** block (current state one-liner + exact next actions + open decisions).
   - [`README.md`](README.md) — append one dated line to the Status Log.
   Then commit (and push if a remote is set). This is how another PC continues without this chat.
2. **Never put secrets in git.** Passwords live in a local, gitignored `CREDENTIALS` file
   (SSH-root, cockpit-admin, WiFi). Also gitignored: `cockpit/api/config.json`, `*.db`,
   `cockpit/api/certs/`, `**/proxy.env`, `dump/*.img`. If you generate creds, hand them to
   the user out-of-band and reference them as "in local CREDENTIALS", never commit them.
3. **Don't break the lifelines / brick the device** (see Safety below) — confirm USB-ADB
   root AND SSH still work before any decommission/delete/flash step.

## How to reach the device
- Device adb serial: `0123456789ABCDEF` (USB). `adb -s 0123456789ABCDEF shell ...`.
- **Cockpit** (new authenticated UI/API): `https://192.168.100.1/` (self-signed) — admin creds in local `CREDENTIALS`.
- **SSH root** (Alpine chroot): `ssh -o PubkeyAuthentication=no root@192.168.100.1` — creds in `CREDENTIALS`.
- Device LAN is `br0 = 192.168.100.1/24` (WiFi `wlan0` + USB `rndis0`); PC gets a DHCP IP there.

## Safety facts (learned the hard way — do not relearn by bricking it)
- **NEVER restart adbd or change `persist.adb.tcp.port` at runtime** → it tears down the
  shared rndis+adb USB gadget and the dongle vanishes from USB until a **physical replug**.
  To close network ADB, use the firewall (7628 is already dropped) — not adbd restarts.
- **USB-ADB root is the ultimate lifeline** (independent of everything; `ro.secure=0`).
  **SSH into the Alpine chroot** is the second root path. Keep both working.
- **Do NOT delete `/system/bin/initmifiservice.sh`** — despite the name it's essential boot
  infra (builds `br0`+`192.168.100.1`, `chmod 666 /dev/smd11`, preserves the adb USB config).
  The "backdoor" is `MifiService.apk` (unauth `:80 /ajax` + OTA) — that's the thing being removed.
- Daemons launched over adb need `setsid` + stdio redirect or they die / hang the shell.
  Use `alpine/start-services.sh` (reliable) to (re)start chroot services.
- Chroot devpts: this 3.10 kernel is single-instance; `/dev/ptmx` must be `mknod c 5 2`
  (handled by `alpine/enter.sh`). Host paths the cockpit needs are bind-mounted in by `enter.sh`.

## On-device layout (what runs where)
- Alpine armv7 chroot at `/data/linux` (rootfs in `/data/linux/rootfs`); enter via
  `/data/linux/enter.sh`; services via `/data/linux/start-services.sh`.
- Services: `sshd :22`, cockpit `:443` (serves `cockpit/web`, modem AT via `/dev/smd11`,
  allow-listed proxy to stock `:80`). tailscale/adguard installed, dormant (need WAN).
- Boot persistence: `/system/etc/init.qcom.post_boot.sh` → `/data/webui/boot-hook.sh` →
  `harden.sh` (firewall) + `start-services.sh`. All reboot-verified.
- Firewall (`webui/deploy/harden.sh`): drop rmnet0-inbound, 7628, and `:80`/`:8088` except
  from localhost (backdoor decommissioned); block phone-home `120.77.62.229`.

## Repo map
`cockpit/api` (Python-stdlib HTTPS API) · `cockpit/web` (SPA) · `alpine/` (chroot scaffolding)
· `webui/deploy` (device scripts: harden/boot-hook/start-webui) · `dump/` (firmware text records;
`.img` gitignored) · docs: `PROGRESS.md`, `SPECS.md`, `SECURITY-AUDIT.md`, `EXPLOITS.md`,
`IP-INVESTIGATION.md`, `CAPABILITIES.md`, `ANTENNA.md`, `*-PLAN.md`.

## Current blocker (as of last session)
Cellular **signal/band mismatch** gates SMS/calls/data — the fix is a directional **B1/B3
LTE antenna** (hardware, owner's side; see `ANTENNA.md`/`CAPABILITIES.md`). Software is at a
clean checkpoint. See `PROGRESS.md` ▶ RESUME HERE for the exact next actions.
