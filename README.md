# 4G USB Dongle — Investigation Log

Living record of what we've learned about the 4G USB dongle. Updated as new info arrives.

## TL;DR
- **Device:** UZ801 4G UFI dongle (WiFi + USB), Qualcomm chipset, firmware `UZ801-V2.3.12`.
- **Admin UI:** http://192.168.100.1/ — login **admin / admin**.
- **Status (2026-11-07):** ✅ Firmware **dumped** (`dump/`), ✅ **security-audited** (`SECURITY-AUDIT.md` + deep 2nd pass; device is remotely rootable), ✅ **exploits + mitigation status** written (`EXPLOITS.md`), ✅ **hardware specs** (`SPECS.md`), ✅ new **web UI now hosted ON the dongle** at `http://192.168.100.1:8088/` (busybox httpd + `/ajax` reverse-proxy; no PC proxy) with an **Identity tab** (IMEI read 1029 / write 2002; IMSI/ICCID read-only), ✅ **dongle hardened** (network ADB 7628 closed, cellular-WAN inbound dropped, phone-home `120.77.62.229` blocked, WiFi password rotated) while **USB ADB root kept**. SIM mystery **solved** earlier: band-20 hardware mismatch (not software).
- **Status (2026-11-06):** ✅ Firmware dumped, security-audited, SIM diagnosed (band-20 mismatch — hardware).
- ⚠️ Device currently left in **ADB-enabled** mode → root `adb` is reachable over the LAN on **TCP 7628**. Revert when done: `adb shell "setprop persist.sys.usb.config rndis" && adb reboot`.

## Device identity
| Item | Value |
|---|---|
| Model | **UZ801** (generic 4G UFI / MiFi) |
| Chipset | Qualcomm |
| Firmware | `UZ801-V2.3.12` |
| IMEI | `8600180460XXXXX` |
| USB ID | `05c6:f00e` (Qualcomm; generic "Android" descriptors) |
| USB mode | RNDIS + CDC-Data (appears as USB Ethernet NIC) |
| Bus power | 500 mA |

## Host networking (Linux)
| Item | Value |
|---|---|
| Interface | `enx0204313XXXXX` |
| Interface MAC | `02:04:31:XX:XX:XX` |
| Host IP (DHCP from dongle) | `192.168.100.129/24` |
| Gateway | `192.168.100.1` (the dongle) |
| Default route | via `192.168.100.1`, metric **101** (secondary; primary is `eno1` → 192.168.0.1, metric 100) |
| DNS handed out | `8.8.8.8` |

## Built-in WiFi hotspot
| Item | Value |
|---|---|
| SSID | `4G-UFI-XXX` |
| Password | `1234567890` (factory default ⚠️) |
| Encryption | WPA (type code 4) |
| WiFi MAC | `a0:2b:2c:XX:XX:XX` |
| Channel / mode | 6 / 802.11n |
| Max clients | 10 |
| LAN IP | `192.168.100.1` |

## Admin web interface
- URL: `http://192.168.100.1/` — Jetty-served panel titled "4G Modem". Login **admin/admin**.
- Languages: English / Chinese / Russian. Copyright 2021–2024.
- **API:** single endpoint `POST http://192.168.100.1/ajax`, body `{"funcNo":N, ...}` (JSON). Status reads work without authentication.

### funcNo command map (reverse-engineered from the UI JS)
| funcNo | Function | Reads / writes |
|---|---|---|
| 1000 | Login | net_mode, fwversion, conn_mode, imei |
| 1001 | Network status | rssi, oper, netstatus, netmode |
| 1002 | IP / DNS info | IP, mask, dns1/2, ssid, wlan_ip, pwd |
| 1003 | Traffic | up_bytes, down_bytes, client_num, maxSta |
| 1004 | Set connection mode | auto / manual |
| 1005 | Set net mode | 0=Auto, 1=3G-only, 2=4G-only |
| 1006–1010 | WiFi settings / security | ssid, mac, channel, pwd, encryp_type |
| 1011, 1052–1055 | MAC address filter | — |
| 1013 / 1014 | Device reboot / reset | (control) |
| 1015 | SIM status | sim_status, imsi, iccid |
| 1016–1018 | WAN / APN profiles | apn profiles, connect |
| 2001 | **Enable USB debug (ADB!)** | switches `persist.sys.usb.config` → `rndis,serial_smd,diag,adb`. Triggered by hidden page `/usbdebug.html` → `js/usbdebug.js` |
| 3000 | **Apply firmware upgrade** | (control — flashes the staged package) |
| 3001 | Delete staged upgrade package | (control) |
| 3002 | Device / firmware info | `device_name=UZ801`, `sw_version=V2.3.12` |

> **Firmware upload** = multipart `POST /ajax`, form field `upload_file` (no funcNo; server sniffs the multipart). The web flow is **push-only**: Upload → `3000` (apply) → `3001` (delete package). There is **no readback/backup/dump** function.
>
> ⚠️ Note (2026-11-06): `1029` (device info), `2000` (reboot→bootloader), `2001` (enable ADB) and `2002` (**write IMEI** via `AT+WRIMEI`) are **not called by the shipped UI JS but DO exist as live server handlers** in `AjaxSevlet.java` (confirmed by the firmware audit — see *Security Audit*). The whole `/ajax` API is **unauthenticated** (login 1000 is cosmetic), so any of these is callable by a raw POST.

## Security notes
- WiFi still uses the **default password `1234567890`** — change it.
- Admin panel uses **default admin/admin** — change it.
- Status endpoints are readable without login (anyone on `192.168.100.0/24` can read IMEI/SIM/config).

## Firmware dump — routes investigated (2026-11-06)
**Goal:** pull the full firmware/flash image off the device.
**SoC (per AlienWolfX/UZ801-USB-MODEM):** Qualcomm **MSM8916** (Snapdragon 410-class), eMMC, stripped **Android 4.4.4**. Target name `msm8916_32_512`. → EDL loader would be an **8916 eMMC Firehose** (`prog_emmc_firehose_8916*.mbn`).

**Initial exposure (RNDIS-only state):** USB = RNDIS + CDC-Data only; network = only TCP/80; web UI push-only. Looked like no software path **until** `/usbdebug.html` was found.

### ✅ DUMP COMPLETE (2026-11-06) — software route, no case-opening
**How:** Hidden page **`/usbdebug.html`** fires **`POST /ajax {"funcNo":2001}`** → USB composition switches to `rndis,serial_smd,diag,adb`. Device re-enumerated as **`05c6:90b6`**; `adb` connected as **`uid=0(root)`** (Android 4.4.4 userdebug/test-keys, fingerprint `qcom/msm8916_32_512/...:4.4.4/KTU84P/eng..20240812`). eMMC = `mmcblk0`, **3.64 GiB** (7634944×512B sectors), 27 partitions.

**Transport gotcha:** this KitKat `adbd` does **not** support `adb exec-out` (returns "error: closed"). Also device `toolbox ls` has no `-1` (use `busybox ls -1`), and `/data` has only ~700 MB free. Working method = **direct `adb pull` of each `/dev/block/bootdevice/by-name/<part>`** (byte-perfect; md5 verified against on-device `busybox md5sum`). Script: `scratchpad/dump_uz801.sh`.

**Result:** 25/25 firmware partitions pulled, 0 failures, all md5-verified → **`dump/`** (953 MB). Skipped `userdata` (2.48 GiB) + `cache` (runtime/user state, not firmware). GPT primary+backup captured. Checksums in `dump/manifest.md5`; log in `dump/dump.log`.

| Image | Size | Identified as |
|---|---|---|
| `sbl1`,`rpm`,`tz`,`hyp`,`aboot` (+`*bak`) | 0.5–1 MiB | ARM **ELF** (QC secure-boot chain; bak == primary) |
| `boot`,`recovery` | 16 MiB | **Android bootimg** (kernel+ramdisk). UART console = `ttyHSL0,115200` |
| `system` | 800 MiB | **ext4** rootfs (mountable) |
| `modem` | 64 MiB | **FAT16** (baseband images + MCFG/NV) |
| `persist`,`splash`,`fsg`,`modemst1/2`,`sec`,`ssd`,`DDR` | — | NV / config / data |
| `misc`,`pad`,`fsc` | — | all-zero (empty) |

**Device left with ADB enabled.** To revert to stock USB mode: `adb shell "setprop persist.sys.usb.config rndis"` then reboot (or factory reset). ADB persists across reboot until reverted.

**Not yet done (optional):** full raw `mmcblk0` image, `userdata`/`cache`, mounting `system.img`, carving `modem.img` (MCFG_SW.MBN etc.), `adb reboot edl` for a bootrom-level dump.

### Hardware fallbacks (were not needed)
1. **EDL via test point** — short D+/GND or the two EDL pads while plugging in → `05c6:9008`; dump with 8916 Firehose. 2. **UART** (`ttyHSL0`, 115200). 3. **eMMC ISP / chip-off**.

## References & community intel

### Operational cheat-sheet (re-root / re-dump)
```bash
# 1. Enable root ADB — no case opening, reversible
curl -s -d '{"funcNo":2001}' http://192.168.100.1/ajax      # == loading /usbdebug.html
adb wait-for-device && adb shell id                          # expect uid=0(root)

# 2. Dump a partition byte-exact, then verify against the device
adb pull /dev/block/bootdevice/by-name/<name> <name>.img
adb shell "busybox md5sum /dev/block/bootdevice/by-name/<name>"   # compare to md5sum <name>.img

# 3. Revert to stock USB mode
adb shell "setprop persist.sys.usb.config rndis" && adb reboot
```
Gotchas (this build): `adb exec-out` is **unsupported** (old KitKat adbd → "error: closed"); device `toolbox ls` has **no `-1`** (use `busybox ls -1`); `/data` has only ~700 MB free (can't dd-to-file the 800 MB `system`). `busybox` is present and is the Swiss-army knife on-device.

### Inspecting the dump
```bash
sudo mount -o ro,loop dump/system.img /mnt     # ext4 Android rootfs
sudo mount -o ro,loop dump/modem.img  /mnt     # FAT16 baseband (or: 7z l / mdir)
```
Inside `modem.img`, the active carrier config is at `IMAGE/MODEM_PR/MCFG/CONFIGS/MCFG_SW/.../MCFG_SW.MBN` — relevant to the SIM/APN problem.

### Intel from AlienWolfX/UZ801-USB-MODEM (MIT; board `FY_UZ801_3.2`) — read & verified
- **Full root** (beyond root-adb): `adb reboot bootloader` → `fastboot boot twrp-3.1.1-0-seed.img` → TWRP install `SR5-SuperSU-v2.82-*.zip`.
- **IMEI repair:** on-device `modem_at "AT+WRIMEI=<imei>"` then reboot. (`modem_at` is a device binary; IMEI is **not** a web funcNo.)
- **APN/MCFG fix:** push a `MCFG_SW.MBN` to `/lib/firmware` + reboot, or edit the one inside `modem`.
- **`/system/bin/initmifiservice.sh`** is the startup glue: sets `persist.sys.usb.config`, loads `xt_HL.ko` + iptables TTL mangle (tether masking), bridges `rndis0`→`br0`=192.168.100.1, runs `modem_at`, `chmod 666 /dev/smd11`. LAN IP is set here (~line 22).
- **EDL:** `adb reboot edl` works on *some* units; OpenWRT path uses `fastboot oem reboot edl`; hardware = short D+/GND or the two EDL pads. Dump/restore with bkerler/edl: `python3 edl rf backup.bin` / `edl rl outdir --genxml`.
- Repo also has **OpenWRT** and **Debian** port notes (wiki, not yet pulled) and references `adbcontrol.zip`, `busybox.apk`, TWRP/SuperSU assets.
- The decompiled web app references `shell.html`/`shell.js` — **not present** on our V2.3.12 (404); only `usbdebug.html` is.

### XDA thread intel — *"Qualcomm MSM 8916 LTE/4G USB Modem (Chinese Firmware)"*
Thread: https://xdaforums.com/t/qualcomm-msm-8916-lte-4g-usb-modem-chinese-firmware.4407691/ (started Feb 2022, 52 posts/3 pages). Live site is Cloudflare-blocked to automated fetch + archive.org refused by proxy; **user supplied the saved HTML** → local copy in `html-forum/`, text in `dump/p{1,2,3}.txt`. Read 2026-11-06. The thread is mostly stuck users; little firmware actually shared — but the hardware/variant intel is solid.

**Hardware (post #52 guide "jorgeolverabrandon"; #10 xontol; #28 root@):**
- Snapdragon 410 / **MSM8916** (4×A53 @1.2 GHz, Adreno 306, Hexagon QDSP6v5), **512 MB LPDDR3 + 4 GB eMMC 5.0**, WiFi/BT = **WCN3620** (2.4 GHz b/g/n), baseband = **Gobi LTE CAT4** (150/50). Kernel Linux 3.10.28.
- **LTE bands: FDD B1/B3/B5/B8 + TDD B40** (some units fewer); factory-tuned for China Telecom/Unicom. → **no B7(2600)/B20(800)/B28** — prime suspect for weak/no signal outside China.
- Telephony: data works; **SMS & voice generally DON'T work**; SMS often absent from the web UI. GPS present but needs config.

**⚠️ Mainboard/firmware variants are NOT cross-flashable** (flashing the wrong one → dead modem / red LED / no tethering): `FY_UZ801_V2.1`, `FY_UZ801_v3.2`, `UFI003_MB_V02` (white vs black units differ). Other strings seen: `UFI_ZX_EL916_HW04_V025_20210727` / `WEB_V1.0.69b#` (HW1.3). Ours = Jetty "4G Modem" **V2.3.12**. Blob name seen: `ufi003-kiyi.bin`; donor FW mentioned: Oppo A31T.

**EDL entry (easier than test-points):** unplug → **hold the recessed reset/boot button** (paperclip hole, or pad on PCB) → plug into USB → release after ~2 s → enumerates as `05C6:9008 QDLoader 9008`.

**⚠️ Per-device calibration partitions — NEVER erase** (unique IMEI / Wi-Fi MAC / NV): `modemst1`, `modemst2`, `fsg`, `fsc`, `persist`. **✅ our `dump/` already has all of these = a safe per-device backup.** Required to restore IMEI/signal if ever wiped.

**A full factory "restoration package" exists** (post #52, Nov 2026): factory GPT + signed bootloaders + `modem.bin` + kernel + 800 MB system + **`prog_emmc_firehose_8916.mbn`** + `flash_stock.py` (EDL flash ~3 min). Download link lives in that thread post/attachment (not captured by the text dump) — grab from the page if a clean factory restore is ever wanted. Post-restore defaults: SSID `4G-UFI-XXXX`, pwd `1234567890`, admin/admin, web also on `:8000`, ADB via `/usbdebug.html` (confirms our method).

**IMEI:** writable in the web UI on *some* versions (password-protected, password unknown) else PC AT tool / on-device `modem_at "AT+WRIMEI=..."`.

**External resources referenced:** GitHub `u0d7i/uz801` (users say stock FW "seems different"); OpenStick (Debian/MSM8916); OpenWrt ports `MIFIB`/`HMUF02`/`HM-MIFIB-V12`; `HandsomeHacker` CSDN blog (Debian port); Coolapk feeds `37834896`/`37542603` (Chinese); bkerler/`edl`.

**→ Relevance to our SIM-Absent problem:** users on the SAME **V2.3.12/V2.3.13** report "Unknown <carrier>" / won't register / "LTE OFFLINE"; **no software fix surfaced in-thread.** Leading causes: (a) **band mismatch** (device lacks the carrier's LTE band), (b) weak RF/antenna (a user needed an external amp). We now have **root ADB + a diag port**, so we can diagnose on-device (AT via `modem_at`, `logcat`/qcril, SIM state) — which most posters couldn't. See *SIM debugging (now that we have root)*.

## SIM diagnosis (live, via root AT — 2026-11-06) ⭐ reframes the problem
Using on-device `modem_at` (AT channel `/dev/smd11`) we queried the modem directly. **The web UI's "SIM Absent" is WRONG** — at the modem level the SIM is fine; the real blocker is **network registration + empty APN**.

| AT query | Result | Meaning |
|---|---|---|
| `AT+CPIN?` | `READY` | SIM present & unlocked |
| `AT+CIMI` | `22605XXXXXXXXXX` | IMSI → **MCC 226 / MNC 05 = Digi Romania** — SIM reads fine |
| `AT+CGSN` | `8600180460XXXXX` | IMEI (matches) |
| `AT+CSQ` | `31,99` | **Excellent signal (~−51 dBm)** — a tower IS audible (first read was `99,99` = modem still warming up) |
| `AT+CREG?`/`+CEREG?`/`+CGREG?` | `0,2` | **Searching, NOT registered** on any network |
| `AT+CGATT?` | `0` | Not attached to packet service |
| `AT+CGDCONT?` | APN `""` / `null` | **No APN configured** |
| `AT^SYSINFO` | `4,0,0,0,1` | No service; SIM valid |
| `AT+CGMR` | `UZ801_V3.0_21_V01R01B10 [Sep 07 2015]` | modem-side firmware build |

### What we did (write ops) + result — SIM problem RESOLVED (explained)
1. Set APN `internet`: `AT+CGDCONT=1,"IPV4V6","internet"` **and** device profile via `{funcNo:1017,no:"1",name:"Digi",apn:"internet",auth:"0"}` → saved ✓.
2. `AT+COPS=0` (auto-register) → modem selected **`"DIGI RO",7`** (LTE); `AT+CREG?` → `1` (registered). Web UI `funcNo 1015` flipped from **"Absent" → "Ready"** (ICCID `89400525XXXXXXXXXXX`).
3. Force LTE-only: `{funcNo:1005,net_mode:"2"}` → ✓ (learned: 1005's param is `net_mode`). Connect via `{funcNo:1018,profile_num:"1"}`.
4. First attach attempts failed (`SIGNAL_LOST`, restricted service). **After the APN was saved + retries it DID attach:** `AT+CGATT: 1`, `AT+CEREG: 0,1`, pulled a real **Digi IP `10.82.161.171`** (+ Digi DNS); web UI → `netstatus: Connected / LTE`.
5. **…but the link is effectively unusable:** 20-ping to `8.8.8.8` = **60% loss, RTT 0.5–7.5 s**; DNS fails (`unknown host google.com`). It drifts in/out of service (`^SYSINFO → 1,3,1,9,1` = LTE, restricted, roaming). Serving cell dropped to a usable-band cell at **−140 dBm**.

### ⛔ ROOT CAUSE — band mismatch (hardware, not fixable in software)
`AT$QCRSRP?` / `AT$QCRSRQ?` measured the serving cells:

| PCI / EARFCN | Band | RSRP | RSRQ | Device supports? |
|---|---|---|---|---|
| 351 / **6425** | **B20 (800 MHz)** | **−44 dBm (very strong)** | −10.7 | ❌ **NO** |
| 171 / 3350 | B7 (2600) | −100 dBm | −20 (unusable) | ❌ no |
| 438/439 / 525 | B1 (2100) | **−137/−140 dBm (dead)** | −20 | ✅ yes, but no signal here |

**The strong Digi cell here is Band 20 (800 MHz), which this dongle cannot use** (supports only FDD B1/B3/B5/B8 + TDD B40). The one supported band with any presence (B1) is at −137/−140 dBm. ⇒ the data bearer is **signal-starved**: it registers and even attaches/gets an IP intermittently, but with ~60% packet loss and failing DNS — **not usable**. SIM, APN, and registration are all fine now; **the radio simply lacks the band Digi uses at this location.** Matches XDA reports (no B20; a user needed an external amp).

**Options (all hardware/coverage, not software):** (a) use where Digi has strong **B1/B3** (urban) coverage; (b) external LTE antenna (helps B1/B7 a little, but can't add B20); (c) a carrier whose local LTE is on B1/B3; (d) accept data won't work here. Net mode left at 4G-only; APN profile "Digi/internet" saved.

## Admin-API to-do (web UI) — mostly done
1. ~~Confirm SIM Ready (1015)~~ ✅ now Ready. 2. ~~Add APN `internet` (1017)~~ ✅ profile #1 saved. 3. ~~Net mode (1005)~~ ✅ 4G-only. Remaining is **RF/band coverage**, see root cause above.

## Status Log
- **2026-11-09, band-lock Phase 2 🟡 — EFS2 reachable but file ops are SECURITY-LOCKED:** Built `diag/qcdiag_efs.py` (EFS2 over the proven `/dev/diag` transport: SUBSYS 0x4B / subsys 0x13). EFS2 **HELLO + QUERY respond** (`QUERY → 4b 13 01 00 03…` = version 3 + FS limits) proving the subsystem is fully reachable — **but every file-access subcommand returns zero bytes**: `STATFS /` (root always exists), `STAT /nv`, `STAT <path>`, `OPEN <path>` all silent. Silence on `STATFS /` (not an errno) ⇒ the modem's **DIAG EFS access is gated behind an SPC/password unlock** (the unlock QPST sends transparently). Next: `DIAG_SPC_F` (0x25, SPC `000000`) → re-try STAT/OPEN/READ the band item, print value (**read-only; stop before WRITE**). The unlock Bash run was blocked by Claude Code's auto-mode classifier (resembles a security bypass) — needs owner approval of that step for this owned/authorized device. See `BANDLOCK-LINUX-PLAN.md` Phase 2.
- **2026-11-09, band-lock Phase 0+1 ✅ (on-device DIAG):** Backed up EFS partitions (`diag/efs-backup/`: fsc/fsg/modemst1/modemst2 + md5). **Proved `/dev/diag` request/response from the Alpine chroot** (`diag/qcdiag_probe.py` → valid Extended Build ID from the modem): `DIAG_IOCTL_SWITCH_LOGGING`=7 via struct `(MEMORY_DEVICE_MODE,0,-1,0)`, 4-byte `0x20` data-type write prefix, HDLC FCS 0x8408, `/dev/diag` bind-mounted into the chroot. Next: EFS2 read of the LTE band item (read-only) before any write. See `BANDLOCK-LINUX-PLAN.md`.
- **2026-11-09, cockpit fixes + Linux band-lock plan:** Fixed SMS inbox (read SIM `SM` storage, not `ME` — 3 received msgs now show) and the **stuck "incoming call"** (CLCC `mode!=0` data bearer was rendered as a voice call; now filtered → `calls:[]`). Also: WiFi recovered via **cold replug** (warm reboots wedge the WCNSS fw); stock app's hostapd blocked by a `/data/hostapd` SELinux mislabel, worked around by starting hostapd directly. Wrote `BANDLOCK-LINUX-PLAN.md` — on-device (no-Windows) LTE band-lock to B1/B3/B5/B8/B40 via `/dev/diag` EFS write (NV 65633) or runtime QMI-over-qmuxd, with full backup/rollback and brick-risk gating.
- **2026-11-09, ⚠️ ndc takeover COMPLETED in code; WiFi AP found broken WITH the stock app too:** Finished `uz-netservices.sh` with the missing driver-level steps — `insmod /system/lib/modules/wlan.ko` (the framework normally does this) → `fwpath=ap` → `ndc softap set/startap` → `brctl addif br0 wlan0` → `ndc tether`. **But testing exposed a deeper problem:** the WiFi AP is currently non-functional **even with `MifiService` enabled** — after clean (warm) reboots `wlan0` stays **DOWN**, never joins `br0`, and `dnsmasq`/tether wedge; kernel log shows `wlan: link not ready` + the driver not entering AP mode. The `netd avc denied write /data/hostapd` lines are a **red herring** (that dir is always mislabeled `system_data_file`; `chcon` denied under enforcing, delete+recreate re-applies the wrong label — and it was never the working path; the real AP used `/data/misc/wifi/hostapd`). This is the owner-reported **"AP drops" instability, now fully down**, very likely **aggravated by the extensive disable/enable/`ndc`/reboot testing wedging netd's WiFi state machine**. **Core path unaffected:** USB tethering + cockpit `:443` (200) + SSH + stock `:80` all healthy. **Recommended recovery: a COLD power-cycle (physical unplug/replug)** to reset the WCNSS WiFi firmware (warm reboots don't clear it); if WiFi still won't start, deeper (a factory reset fixes labels/state but wipes cockpit/chroot/hardening — re-provisionable). The ndc takeover code is complete + committed, ready for when the WiFi subsystem itself is healthy. ⇒ delete-the-app remains blocked (now by WiFi being broken even with the app, not just the takeover).
- **2026-11-09, `ndc` SoftAP takeover — PARTIAL (DHCP ✅, WiFi radio ❌):** Built the netd-driven takeover into `uz-netservices.sh` (extracted exact KitKat syntax from `strings /system/bin/netd`): `ndc softap fwreload/set/startap` + `ndc tether start/interface add`. Rehearsal (app `pm disable`d + reboot): **`ndc tether` brings `dnsmasq`/DHCP up ✅** (PC got a lease, cockpit 200, SSH up, LAN-over-USB works) and the `hostapd` process starts — **but the WiFi radio is not a usable AP:** the stock app/netd also **load the WLAN kernel driver in AP mode** (`/sys/module/wlan/parameters/fwpath=ap`) and **bridge `wlan0`→`br0`**, and standalone `ndc softap fwreload` fails at boot (driver not loaded → `fwpath` missing, `wlan0` not in `br0`). ⇒ wireless clients wouldn't work with the app removed. Re-enabled + rebooted → **fully restored** (`fwpath=ap`, hostapd/dnsmasq/`:80`/cockpit/SSH all back). **Verdict: `MifiService.apk` still NOT cleanly deletable** — the remaining piece is driver-level (WLAN AP-mode load + `wlan0`→`br0` bridge). Backdoor is already neutralized, so keeping the app costs nothing; the deeper driver RE is low-value for now.
- **2026-11-09, 🧪 delete DRESS REHEARSAL — `pm disable` + reboot → FAILED (reversible, restored):** Ran the reversible test before any delete. With `com.mifiservice.hello` disabled and rebooted: cockpit `:443`, SSH `:22`, and `br0`/`192.168.100.1` survived, **but `hostapd` (WiFi AP) and `dnsmasq` (DHCP) did NOT come up**, and `uz-netservices.sh` **could not start them** (`FAIL hostapd/dnsmasq`). Root cause: SoftAP bring-up needs **netd's** interface/mode/firmware orchestration (which the stock app triggers via the framework) — a bare `hostapd -B` / `dnsmasq` isn't sufficient. ⇒ **the app is NOT safely deletable yet.** Re-enabled (`pm enable`) + rebooted → **fully restored** (hostapd+dnsmasq+`:80`+cockpit+SSH all back, PC got a DHCP lease, cockpit 200). **Prerequisite before deletion:** rewrite the takeover to drive **netd via `ndc softap`/`ndc tether`** (set AP params, `startap`, tether DHCP) instead of launching hostapd/dnsmasq directly, then re-run this rehearsal. App stays (decommissioned/firewalled — backdoor already neutralized) until then.
- **2026-11-09, delete pre-flight for `MifiService.apk`:** readiness-checked before removing the Chinese backdoor. ✅ Lifelines (USB-ADB root + SSH) green; ✅ backed up the APK → `/data/backups/MifiService.apk` (md5 `74f5280b…`, also recoverable from the `system.img` dump); ✅ scope confirmed — the backdoor is **`MifiService.apk` only** (other `/system` apps are standard Android/Qualcomm); ✅ EDL restore set present (25 imgs). Found + fixed a bug in `uz-netservices.sh` (used `pgrep/pidof/dirname` absent on the Android host shell → mis-detected hostapd/dnsmasq and tried to double-start them; now busybox-safe `proc_running()` → verified **clean all-OK no-op** while the app runs) and **wired it into `boot-hook.sh`**. **Remaining gate before the actual delete:** a reversible `pm disable com.mifiservice.hello` + reboot test to prove WiFi/DHCP/cockpit/SSH all survive via the takeover (revert with `pm enable` + reboot, or proceed to APK removal).
- **2026-11-09, band-lock ruled out via software + diagnostics added:** Investigated the NV/QMI band-lock path (owner said "yes"). Verdict: **not cleanly feasible on this modem** — no AT band-lock verb (confirmed earlier); **libqmi/`qmicli` can't reach it** (internal modem over SMD+`qmuxd`, no `cdc-wdm`, no QRTR — no device node for userspace QMI); NV 65633 `lte_bc_config` is an EFS item editable only via a custom **diag** write with no off-the-shelf Linux tool and real brick risk → declined per "don't brick it". **And likely wouldn't help anyway:** live `$QCRSRP` showed strong **usable B3 (EARFCN 1400 @ −69 dBm)** yet `CGATT:0` / `CEREG:0,3` DENIED — the problem is intermittent **EMM attach failure**, not band choice (`+CEER` only says "EMM attach failed"). Built two read-only diagnostics (`diag/signal-monitor.sh`, `diag/wifi-watch.sh`) and verified them live: the modem **oscillates** registered↔denied (caught it `CEREG=1-home CGATT=1` on **B1 525 @ −88 dBm**, correctly flagging the stronger `B7:3350@−69` as unusable); WiFi AP is up but device **load ≈ 4.9 on 2 cores** / temp 56 °C — candidate causes for the reported hotspot drops. Next: soak with both monitors to characterize the intermittent attach + AP drops.
- **2026-11-07, host routing:** added `host/setup-host-routing.sh` + `host/README.md` (PC side, NetworkManager) — dongle iface pinned to name `uz801` by USB ID 05c6:90b6, NM profile with route metric 2000 / DNS priority 200, so the dongle is only the last-resort default gateway.
- **2026-11-07, SMS fast-fail + band-lock feature:** cockpit `sms_send` now does a registration pre-check and aborts the CMGS prompt protocol if the `>` never arrives (no more ~30s hang + cryptic `+CMS ERROR: 500` when signal-gated); API returns **409** not-registered. Added a **LTE band-lock** feature end-to-end — new `GET/POST /api/band` (modem `band_lock`/`band_status` + Qualcomm LTE bitmask: E-UTRA band N → bit N-1) and a **Band lock** card in the Network tab (pick B1/B3/B5/B8/B40 or Auto; shows the live serving band + whether it's usable, e.g. flags the strong-but-unusable B20). The band-lock **AT command is Qualcomm-modem-specific and unverified on this board**, so it's config-driven (`platform.band_at`, empty by default → nothing written, POST returns 501 with setup guidance) exactly like `netmode_at`. All verified against the mock modem (mask math + GET serving/cells + 501 path). **Then probed the live modem (`modem_at` + `AT+CLAC`) — verdict: this firmware (`UZ801_V3.0_21_V01R01B10`, Longsung/Huawei `^SYSCONFIG`/`^PREFMODE`/`$QCNSP` dialect) exposes NO per-LTE-band lock over AT:** `$QCBANDPREF` is 2G/3G/WCDMA-only here (enum ends at `46.Any`, no LTE bands); `AT!BAND`/`AT^SETBAND`/`AT^SYSCFGEX`/`$QCRAT` all ERROR; the full 32-command `+CLAC` surface has no LTE-band verb (only RAT/mode via `^SYSCONFIG?`=`11,0,1,2` and `$QCNSP=?`=`(0,1,2,6),(0-2),(0-3)`). ⇒ **band-lock is not possible on this modem via AT** (would need NV 65633 / QMI). The cockpit feature stays as a serving-band **display** (GET `/api/band` shows the live band + usable flag + visible cells) with the POST a no-op (`configured:false` → 501); band selection on this unit is always "auto" across B1/B3/B5/B8/B40, and B20 is a hardware (antenna) limit regardless — so the real fix remains the directional B1/B3 antenna.
- **2026-11-06, initial scan:** SIM **Absent**, RSSI 0, −120 dBm, Disconnected, 0 APN profiles, 0 WiFi clients.
- **2026-11-06, after SIM insertion (first re-poll):** still **Absent** — detection pending, polling.
- **2026-11-06, firmware-dump recon:** Mirrored the full web UI + JS. Confirmed funcNo map (firmware = 3000/3001/3002, upload via multipart). Port-scanned gateway → only TCP/80. USB exposes RNDIS only (no diag/ADB).
- **2026-11-06, ADB route found (via AlienWolfX/UZ801-USB-MODEM):** SoC = MSM8916 / Android 4.4.4. Hidden page `/usbdebug.html` → `funcNo 2001` enables `rndis,serial_smd,diag,adb`.
- **2026-11-06, ✅ FIRMWARE DUMPED:** Fired `2001` → root ADB → pulled all 25 firmware partitions (md5-verified) to `dump/` (953 MB), skipping userdata/cache. Device left in ADB mode. See *Firmware dump → DUMP COMPLETE*.
- **2026-11-06, XDA thread mined:** User supplied saved HTML (`html-forum/`). Extracted bands (FDD B1/B3/B5/B8 + TDD B40), mainboard variants (not cross-flashable), EDL reset-button method, calibration-partition list, factory restoration-package pointer. See *References → XDA thread intel*.
- **2026-11-06, ⭐ SIM re-diagnosed via root AT:** Modem says SIM **READY** (IMSI 22605XXXXXXXXXX = Digi RO). Web UI "SIM Absent" is misleading. Set APN `internet` + `COPS=0` → registered to DIGI RO on LTE; web UI now shows **Ready**. But data attach fails `SIGNAL_LOST`.
- **2026-11-06, ✅ SIM ROOT CAUSE found:** `AT$QCRSRP?` → strong Digi cell is **Band 20 (800 MHz) @ −44 dBm**, which this dongle **can't use** (only B1/B3/B5/B8+B40); supported B1 is at −137 dBm (dead). **Band mismatch = hardware limit**, not software. SIM/APN/registration all fixed. See *SIM diagnosis → ROOT CAUSE*.
- **2026-11-06, ✅ NEW WEB UI built (`webui/`):** Modern dark-theme replacement management UI (vanilla HTML/CSS/JS, no build step, no CDN). Runs locally via a Python 3 stdlib reverse proxy `webui/proxy.py` that serves the SPA and forwards `POST /ajax` (JSON + multipart firmware uploads, + session cookie) to the device, dodging CORS/mixed-content. Open `http://localhost:8088/` after `python3 proxy.py` (device IP via `DONGLE_URL` env var, default `192.168.100.1`). Tabs: Dashboard (live poll of 1001/1002/1003/1015 with signal bars/dBm, pause toggle), Network (1004/1005), SIM/APN (1015/1016-1018), WiFi (1006/1009), MAC Filter (1052-1055), Firmware (3002 + upload + 3000/3001), Developer (2001 ADB, 1013 reboot, 1014 reset, raw funcNo console), Settings. Destructive actions gated by confirm dialogs. **Verified end-to-end against the live device** — proxy forwarded `funcNo 1001` and got `{"oper":"DIGI RO","netstatus":"Connected","netmode":"LTE",...,"flag":"1"}`. Unverified funcNos (APN 1016-1018, MAC filter, conn-mode 1004) flagged in-UI and in `webui/README.md`.
- **2026-11-06, SIM outcome refined:** it now *does* attach (IP `10.82.161.171`) but is **unusable** — 20-ping 8.8.8.8 = 60% loss / multi-second RTT, DNS fails. Confirms the band-20 starvation diagnosis (earlier "won't attach" was premature; corrected above).
- **2026-11-06, 🔒 SECURITY AUDIT done (`SECURITY-AUDIT.md`):** firmware is **remotely rootable** — unauthenticated system-privileged `/ajax`, ADB-root by default, unsigned OTA, cleartext phone-home to a Chinese cloud host leaking IMEI. **Verified live:** TCP **7628 is OPEN** and `adb connect 192.168.100.1:7628` reaches adbd → root ADB on the LAN right now (side effect of our debug mode; revert with `setprop persist.sys.usb.config rndis` + reboot). See *Security Audit*.
- **2026-11-07, 🔬 DEEP AUDIT (2nd pass, `SECURITY-AUDIT.md` → "Deep audit"):** decompiled `MifiService` OTA fully — `120.77.62.229:8080` is a **direct server-driven remote-flash channel** (md5 "check" is the server's own value; test-key recovery), i.e. the host can push persistent firmware with no MITM. Also: 3 more setuid-root bins (`procmem`/`procrank`/`librank`), busybox 1.31 ships `inetd`/`crond`/`telnetd`, otacerts.zip = public AOSP test-key, system-uid can relax SELinux. Only one C2 endpoint exists.
- **2026-11-07, 🖥️ UI MOVED ONTO THE DONGLE (`webui/` @ `:8088`):** replaced the PC-side Python proxy with **busybox httpd on the device** (`/data/webui`, started by `start-webui.sh`) that serves the SPA and **reverse-proxies `/ajax` → 127.0.0.1:80** (same-origin, no CORS, no PC). Reachable at `http://192.168.100.1:8088/` from any LAN host. Added **Identity & Modem tab** (IMEI read `1029` / write `2002`=`AT+WRIMEI`; IMSI/ICCID read-only `1015` with note that IMSI is SIM-side). Fixed WiFi save to real funcNos (SSID `1007`, security `1010`). *Gotcha learned:* daemonized httpd must redirect stdio or it hangs the launching adb shell; and **never restart adbd / change `adb.tcp.port` at runtime** — it tears down the shared rndis+adb USB gadget (needs a physical replug; happened once, recovered).
- **2026-11-07, ✅ BACKDOOR DECOMMISSIONED (not deleted):** Walled off the Chinese backdoor without breaking function. Mapped the plumbing: `hostapd`+`dnsmasq` are children of **`netd`** (init-started), NAT = `MASQUERADE -o rmnet0`+`ip_forward=1` (netd), bridge+`192.168.100.1` from `initmifiservice.sh` (a boot script — KEEP; it also `chmod 666 /dev/smd11` and preserves our `persist.sys.usb.config` adb value). The backdoor is specifically `MifiService.apk`'s unauth `:80 /ajax` + OTA. Decommission applied via `harden.sh`: **`:80` and `:8088` DROP'd except from localhost** (authenticated cockpit `:443` proxies `:80` over `lo`), old `:8088` shim stopped + removed from boot-hook, OTA IP already blocked. Verified: `:80`/`:8088` unreachable from LAN (`000`), cockpit + SSH still work, persists across reboot. Root unaffected (USB-ADB + SSH independent of the app — see analysis). Built + deployed a cockpit self-sufficiency layer (`uzplatform.py`, `prefer_direct`): WiFi config (read/write `hostapd.conf`), clients (dnsmasq.leases+arp), traffic (`/proc/net/dev`), device info, netmode, MAC filter — all DIRECT, stock `:80` only a fallback. **Verified direct reads live** (WiFi ssid/pw, clients, traffic, system). Architectural note: the cockpit runs in the Alpine chroot, so host paths/binaries must be bind-mounted in — added `/data/misc` bind (fixed WiFi read). **Remaining before the app can actually be DELETED:** host-side plumbing takeover (`cockpit/api/deploy/uz-netservices.sh` — hostapd/dnsmasq/NAT/data, must run in host context from the boot-hook) + WiFi-restart/getprop need host `/system`+props exposed to the chroot. The DECOMMISSION itself is complete and safe; deletion is a later, tested step.
- **2026-11-07, ✅ COCKPIT + ALPINE STACK DEPLOYED, auto-start on boot:** Built an Alpine armv7 chroot on the dongle (`/data/linux`, `alpine/enter.sh`) with python3/openssh/openssl/tinyalsa/tailscale/adguardhome. Fixed chroot PTY (single-instance devpts on this 3.10 kernel → tmpfs `/dev` + `mknod ptmx c 5 2`). **SSH root lifeline** on `:22`. Built & deployed a new **authenticated cockpit** (`cockpit/api` Python-stdlib HTTPS on `:443` serving `cockpit/web` SPA) — auth+TLS+CSRF, modem via `/dev/smd11` AT, allow-listed stock-`/ajax` proxy (blocks 2000/2001/3000). Verified live from PC: setup/login/status/**real SIM (IMEI/IMSI/ICCID READY)**. SMS send + call dial are code-complete but return the modem's own errors (`+CMS ERROR 500` / dial `ERROR`) until the modem **registers** (signal-gated). All services (stock UI `:8088`, firewall, SSH `:22`, cockpit `:443`) **auto-start via boot-hook → `start-services.sh`, confirmed across a reboot.** (Admin/SSH credentials were generated at deploy and handed to the owner out-of-band — not stored in the repo.) Stock backdoor UI intentionally kept until the decommission phase. Audio voice-bridge still off (needs tinymix spike + a registered call).
- **2026-11-07, ✅ BOOT HOOK VALIDATED + SIM re-detected:** installed boot hook in `/system/etc/init.qcom.post_boot.sh` → `/data/webui/boot-hook.sh` (runs `start-webui.sh` + `harden.sh`). Did a clean `adb reboot`: confirmed the UI (`:8088`) and firewall (`rmnet0`/7628/phone-home drops) **auto-started under SELinux-enforcing** (`boot-hook.log` proves it). Also: after a SIM **hot-insert the modem reports "SIM not inserted" and a CFUN cycle does NOT fix it** — the UICC is only brought up by the boot `usb_uicc_*` services, so a **reboot is required** to detect a freshly-inserted SIM. Post-reboot: `+CPIN: READY`, IMSI 22605XXXXXXXXXX (Digi RO), UI shows SIM **Ready**. Data still **Disconnected** — same **Band-20 mismatch** (`$QCRSRP` EARFCN 6425/B20 @ −44 dBm, unusable; supported bands dead here).
- **2026-11-07, 🔐 DONGLE HARDENED (`webui/deploy/harden.sh`, policy = keep WiFi + change password):** network ADB **closed** (`persist.adb.tcp.port=-1` + iptables DROP 7628; USB ADB root kept as lifeline), **cellular-WAN (`rmnet0`) new inbound dropped**, **phone-home `120.77.62.229` blocked** (OUTPUT DROP), **WiFi key rotated** off default `1234567890` (WPA2-CCMP via funcNo 1010). No `physdev`/`ebtables` on this kernel → can't split WiFi-vs-USB by interface; USB-host MAC changes each reboot → used interface/port rules instead. Firewall is **runtime-only**: boot-persistence needs a one-line edit to `/system/etc/init.qcom.post_boot.sh` (staged `boot-hook.sh`) — **blocked by tooling pending owner approval**; re-apply after reboot via `adb shell /system/bin/sh /data/webui/harden.sh`. See `EXPLOITS.md` for per-backdoor status. **Residual (by owner's choice):** WiFi admin + unauth `/ajax` stay reachable to anyone with the new WPA2 key.

## Security Audit — 2026-11-06 (static, no execution)
Full static backdoor/security audit of the dumped images written to [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md). Analysis only: read-only extract / `strings` / `grep` / jadx decompile; no extracted binary/APK was run. Headline findings:
- **CRITICAL — `/ajax` has NO authentication.** `AjaxSevlet.java` dispatches every funcNo with no session/cookie check (login funcNo 1000 is cosmetic). Jetty binds `0.0.0.0:80`; the app runs as `android.uid.system`. Any WiFi/USB-adjacent host can call any function by raw POST.
- **CRITICAL — funcNo 2001** (`setprop persist.sys.usb.config rndis,serial_smd,diag,adb` + reboot) is unauthenticated → root ADB.
- **CRITICAL — ADB root by default:** `ro.secure=0`, `ro.debuggable=1`, usb config includes `adb`, and `persist.adb.tcp.port=7628` (likely root ADB over TCP reachable on the LAN).
- **CRITICAL — unsigned firmware flash:** unauthenticated multipart upload → `/data/update.zip`; funcNo 3000 → `Updater.doUpdateNoMd5()` flashes with no md5/signature. Recovery verifies only against the **AOSP test-key** (public) → anyone can sign an OTA.
- **HIGH — cleartext-HTTP OTA phone-home** to `http://120.77.62.229:8080/file/...` (Aliyun) every 30 min, leaking IMEI; MITM → remote code execution. Only external endpoint in the whole image.
- **HIGH** — funcNo 2002 writes arbitrary IMEI (`AT+WRIMEI`); funcNo 2000 reboots to bootloader; unauth leak of WiFi PSK / IMSI / ICCID / client list; test-keys signing.
- **MEDIUM** — setuid-root `su` & `tcpdump` in `/system/xbin` (06755); default creds admin/admin + WiFi `1234567890`; DIAG exposed + `/dev/smd11` 0666; unauth reboot/factory-reset/DNS-change; root UART console.
- Non-findings: no web shell-command injection (`exec(String)`, no shell), no extra Unix accounts, clean `hosts`, benign `cdrom_install.iso`.
- Correction to earlier notes: funcNo **2002 (IMEI write) and 1029 (device info) DO exist** as live server handlers in `AjaxSevlet.java`, though the shipped UI JS doesn't call them.
