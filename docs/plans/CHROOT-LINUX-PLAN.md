# Plan — Debian (chroot) on the UZ801, with real root

**Goal:** a real GNU/Linux userland running in a **chroot** on the dongle, with
**genuine root** (we already have host root via adb/`su`), running alongside Android so
the **modem + web UI keep working**. This is Option A from `LINUX-OPTIONS.md`.

**Status: PLAN ONLY — nothing will be installed until you approve.** When you say go,
I execute the numbered steps.

---

## Why chroot (not proot)
We have real root, so `chroot` gives **real uid 0 on the real kernel** (iptables, raw
sockets, mounts, etc.) — not proot's emulated root. Shares Android's **3.10 kernel**.

## Constraints that shape the plan (from `SPECS.md` + live checks)
- **32-bit** userspace (armv7l) → use an **armhf** rootfs (not arm64).
- **Storage:** `/data` has ~700 MB free → minimal rootfs only (Debian ~300–400 MB, Alpine ~50 MB).
- **`/data` is mounted `nosuid,nodev`** → device nodes created under `/data` won't work and
  setuid binaries won't elevate. We work around both by **bind-mounting the real `/dev`,
  `/proc`, `/sys`** into the chroot, and by entering **already as root** (so setuid is moot).
  `ping` etc. run fine as root (or use `busybox ping`).
- **SELinux Enforcing** — in practice broad on this `userdebug` build; most userland works.
  A few kernel-level ops may be confined. Acceptable for a normal Linux box.
- **No systemd** — start services manually or via a light init (Debian: run them by hand /
  a start script; Alpine: `openrc`/`runit`).

## Decisions — FINALIZED (2026-11-07)
1. **Distro: Alpine armhf** (~60 MB, `apk`, musl) — chosen for the ~500 MB of headroom.
2. **Access:** `adb shell` → chroot script **+ SSH** (openssh/dropbear) on the LAN.
3. **Autostart:** **yes** — via the existing `/data/webui/boot-hook.sh`.
4. **Location:** `/data/linux/`.
5. **Stack to install:** openssh, python3+pip, **Tailscale** (remote access; CGNAT-friendly),
   **AdGuard Home** (DNS ad-block), and the **cockpit / secure-API** service
   (see `COCKPIT-API-PLAN.md`).

> Alpine specifics baked into the steps below: use `apk` not `apt`; enable the
> **community** repo; `apk add gcompat` only if a glibc-only binary needs it; Python
> C-extension wheels may compile from source (pure-Python/stdlib — our case — is fine).
> In a chroot there is **no init** (no systemd/OpenRC) → daemons are launched directly
> from `boot-hook.sh`.

---

## Steps (Debian armhf shown; Alpine noted)

### 1. Get a rootfs tarball (on the PC)
No `debootstrap` on the device, so stage a prebuilt **armhf** rootfs on the PC first, into
its own empty dir (treated as untrusted download):
- **Debian:** build a minimal armhf rootfs with `debootstrap` + qemu on the PC, or grab a
  known-good minimal Debian/Ubuntu armhf rootfs tarball; **Alpine:** download the
  `alpine-minirootfs-*-armhf.tar.gz` (tiny).
- Verify checksum; keep it in a dedicated dir, don't run anything from inside it.

### 2. Push + extract on the device
```
adb shell "mkdir -p /data/linux/rootfs"
adb push rootfs-armhf.tar.gz /data/linux/
adb shell "cd /data/linux/rootfs && busybox tar xzf ../rootfs-armhf.tar.gz && rm ../rootfs-armhf.tar.gz"
```

### 3. Enter script (`/data/linux/enter.sh`) — bind mounts + chroot as root
```sh
#!/system/bin/sh
R=/data/linux/rootfs
busybox mount -o bind /dev      $R/dev
busybox mount -o bind /dev/pts  $R/dev/pts
busybox mount -t proc proc      $R/proc
busybox mount -t sysfs sysfs    $R/sys
busybox mount -o bind /dev/pts  $R/dev/pts
printf 'nameserver 8.8.8.8\nnameserver 193.231.252.1\n' > $R/etc/resolv.conf
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
busybox chroot $R /bin/bash   # Alpine: /bin/sh
```
Run as root: `adb shell "su -c '/system/bin/sh /data/linux/enter.sh'"` (adb is already root
here). You land at a **root shell inside Debian**.

### 4. First-run inside the chroot
- Debian: `apt update && apt install -y <pkgs>` (DNS + time must be sane).
- Alpine: `apk update && apk add <pkgs>`.

### 5. (Optional) SSH access over the LAN
- Install `openssh-server` (or `dropbear`), set a root password/key, start it.
- Reachable at `192.168.100.1:22` on WiFi/USB. **Firewall note:** our hardening leaves LAN
  admin open (per your policy) and only drops `rmnet0` inbound + 7628; SSH on 22 is
  **not** exposed to the cellular side (rmnet0 inbound is dropped), only to the LAN — good.

### 6. (Optional) Autostart at boot
Append to `/data/webui/boot-hook.sh` (our existing, approved persistence hook): mount the
binds + start sshd. Survives reboot like the UI/firewall.

### 7. Teardown (fully reversible)
```
# exit chroot, then:
adb shell "busybox umount /data/linux/rootfs/dev/pts /data/linux/rootfs/dev \
           /data/linux/rootfs/proc /data/linux/rootfs/sys; rm -rf /data/linux"
```
Remove the boot-hook lines if autostart was added.

---

## Risk / reversibility
- **Low risk, fully reversible** — it's files under `/data` + bind mounts; nothing in
  `/system`, no flashing, Android/modem untouched. Worst case: `rm -rf /data/linux`.
- Does **not** affect the firewall, the web UI, ADB, or the modem.

## What I need from you to proceed
Answers to the 4 **Decisions** above (distro / access / autostart / location). Default if
you just say "go": **Debian armhf, enter-via-adb + SSH, autostart yes, /data/linux**.
