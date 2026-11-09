# UZ801 4G Dongle — Project Progress & State

Consolidated status of the reverse-engineering, security, and custom-firmware
work on a **UZ801 4G USB/WiFi dongle** (Qualcomm MSM8916, Android 4.4.4).
Living log of day-to-day steps is in [`README.md`](README.md); deep docs are linked below.

> **Credentials are NOT stored in this repo.** The SSH-root and cockpit-admin
> passwords were generated at deploy time and handed to the owner out-of-band.
> Change them anytime (SSH: `passwd`; cockpit: Settings tab).

---

## ▶ RESUME HERE (last updated 2026-11-09)
**State:** Clean checkpoint. Backdoor **decommissioned** (not deleted), cockpit (API+UI)
deployed & auto-starting on boot, SSH + USB-ADB root lifelines working, everything committed
& pushed (`origin` = private `uz801-liberated`). See §5 for the full status table.

**2026-11-09 LATEST — WiFi AP is currently BROKEN (even with the stock app); needs a COLD
power-cycle.** During the app-delete work the WiFi hotspot stopped coming up: after warm reboots
`wlan0` stays DOWN, never joins `br0`, `dnsmasq`/tether wedge (kernel: `wlan: link not ready`).
This is the owner-reported "AP drops" instability, now fully down, likely aggravated by extensive
disable/enable/`ndc`/reboot testing wedging netd's WiFi state machine. **USB/cockpit/SSH/cellular
are all fine.** FIRST ACTION on resume: **physically unplug+replug the dongle** (warm reboots don't
reset the WCNSS WiFi firmware), then check `wlan0` comes up as an AP (`diag/wifi-watch.sh`, or
`cat /sys/class/net/wlan0/operstate` + `ls /sys/class/net/br0/brif/`). The `ndc` SoftAP takeover
(`uz-netservices.sh`: insmod wlan.ko→fwpath=ap→ndc softap→brctl addif→ndc tether) is CODE-COMPLETE
and waits for a healthy WiFi subsystem. Delete-the-app is blocked until WiFi works again.

**2026-11-09 update — software band-lock RULED OUT, diagnostics added.** The NV/QMI band-lock
is **not cleanly feasible on this modem**: no AT band-lock verb; `libqmi`/`qmicli` can't reach
it (internal modem over SMD+`qmuxd`, no `cdc-wdm`/QRTR); NV 65633 `lte_bc_config` is EFS-only via
a custom diag write (no tool, brick risk) → declined. And it likely wouldn't help: live data
showed strong **usable B3 (−69 dBm)** yet attach still failed (`+CEER: EMM attach failed`), so the
blocker is intermittent **EMM attach failure**, not band choice. Added read-only diagnostics
`diag/signal-monitor.sh` (registration/band/CEER logger) + `diag/wifi-watch.sh` (AP-drop + CPU/temp
logger), both verified live — modem oscillates registered↔denied (seen reg'd on **B1 @ −88 dBm**);
WiFi up but **CPU load ≈ 4.9 / 2 cores** (likely behind the hotspot drops).

**The one blocker:** cellular **signal/band mismatch** (supported B1/B3 are weak, strong cell
is unusable B20) → SMS/calls/data don't work yet. **Not software-fixable** — needs a directional
**B1/B3 LTE antenna** (hardware; `ANTENNA.md` + `CAPABILITIES.md`). The cockpit's own features
(WiFi/clients/traffic/SIM/signal reads) work regardless.

**Next actions (pick up here):**
1. *(hardware, owner)* Fit the directional antenna → modem registers → then I can verify/finish
   **SMS send, voice call, and the voice audio-bridge spike** (tinymix routing during a live call),
   and bring up **Tailscale/AdGuard** (need WAN).
2. *(optional, low-risk software)* ~~SMS fast-fail timeout~~ ✅ done (2026-11-07 — `sms_send`
   registration pre-check + prompt-timeout abort; API returns 409 not-registered).
   ~~LTE band-lock feature~~ ✅ built end-to-end (2026-11-07 — `GET/POST /api/band`, Network-tab
   "Band lock" card, Qualcomm LTE bitmask) **and probed on-device: this modem's firmware exposes
   NO LTE band-lock AT command** (`$QCBANDPREF` 2G/3G-only; `!BAND`/`^SETBAND`/`^SYSCFGEX`/`$QCRAT`
   ERROR; `+CLAC` has none), so the card is **display-only** and `platform.band_at` stays empty.
   A real band-lock on this unit would require NV 65633 (`lte_bc_config`) editing via QMI/QCN — a
   separate, riskier effort; the scaffolding is kept for any future modem that does expose the AT
   verb. (Note B20 is unusable due to missing 800 MHz hardware, so band-lock wouldn't fix it anyway.)
   Still open: expose host `/system`+property area to the chroot so cockpit WiFi-**write**/restart
   + `getprop` work directly (finishes self-sufficiency).
3. *(deferred — owner said "not now")* **WiFi AP drops**: owner observes the hotspot dropping.
   Run a performance/stress test to diagnose (hostapd stability/logs, channel/interference,
   throughput soak via iperf3, many-client test, thermal/CPU, USB-gadget contention). Keep the
   USB-ADB + SSH lifelines up; never restart adbd during the test.
4. *(BLOCKED — driver-level piece unsolved)* **Delete the stock app**: two rehearsals done.
   The `ndc`-based takeover (`uz-netservices.sh`) now gets **DHCP/`dnsmasq` up via `ndc tether`
   ✅** and cockpit/SSH/LAN-over-USB survive the app being disabled — **but the WiFi *radio* does
   not**: the stock app/netd **load the WLAN kernel driver in AP mode** (`fwpath=ap`) and bridge
   `wlan0`→`br0`; standalone `ndc softap fwreload` fails at boot (driver not loaded), so wireless
   clients wouldn't work with the app gone. ⇒ **not cleanly deletable yet.** Remaining work =
   replicate the **WLAN-driver AP-mode load + `wlan0`→`br0` bridge** (find how it's `insmod`/loaded
   at boot). Lifelines, APK backup (`/data/backups`), EDL restore set all in place. Since the
   backdoor is already neutralized (decommissioned/firewalled), keeping the app is zero-cost
   security-wise — this is completeness-only and low priority.

To resume on another PC: `git pull`, read `CLAUDE.md` + this block, connect to the device
(`adb -s 0123456789ABCDEF` / cockpit `https://192.168.100.1` / `ssh root@192.168.100.1`;
creds in local `CREDENTIALS`).

---

## 1. What this project did (high level)
1. **Rooted** the dongle via its own stock backdoor (unauth web funcNo 2001 → ADB), one-time.
2. **Dumped** all 25 firmware partitions (md5-verified) — see [`dump/`](dump/) (images gitignored; text records kept).
3. **Security-audited** the firmware (two passes) — [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md), [`EXPLOITS.md`](EXPLOITS.md), [`IP-INVESTIGATION.md`](IP-INVESTIGATION.md).
4. **Hardened** it (closed remote ADB, cellular-WAN inbound, phone-home) while keeping USB-ADB root.
5. Built a **new authenticated "cockpit"** (API + dashboard) to replace the insecure stock web UI.
6. Stood up an **Alpine Linux chroot** on the device (python/ssh/tailscale/adguard + the cockpit).
7. **Decommissioned the Chinese backdoor** (unauth `:80`/OTA walled off) — app still present, not yet deleted.
8. Diagnosed the SIM/data: works, but **signal-gated by a band mismatch** (hardware) — see [`CAPABILITIES.md`](CAPABILITIES.md), [`ANTENNA.md`](ANTENNA.md).

## 2. Hardware (summary; full detail in [`SPECS.md`](SPECS.md))
MSM8916 (Snapdragon 410-class), 2×Cortex-A53 ≤1.19 GHz, Adreno 306, ~388 MB usable RAM,
~4 GB eMMC, Android 4.4.4 / kernel 3.10, LTE **FDD B1/B3/B5/B8 + TDD B40** (no B20/B7),
WCN3620 WiFi/BT, USB **gadget-only** (no host/OTG). IMEI/IMSI/ICCID readable; SMS supported.

## 3. On-device architecture (current)
```
Android 4.4.4 (host)
├─ init → adbd (USB, ROOT; TCP 7628 disabled)         ← our root lifeline over USB
├─ MifiService.apk  :80  stock unauth API  ← DECOMMISSIONED (localhost-only via firewall)
├─ netd → hostapd (WiFi AP) + dnsmasq (DHCP) + NAT(rmnet0)   ← stock plumbing (kept for now)
├─ initmifiservice.sh (boot) → br0=192.168.100.1, chmod 666 /dev/smd11   ← essential, keep
├─ firewall (UZSEC) : drop rmnet0-inbound, 7628, :80/:8088 (LAN), block phone-home
└─ /data/linux  = Alpine armv7 chroot
     ├─ sshd            :22    ← root SSH lifeline (independent of stock app)
     ├─ cockpit API     :443   HTTPS, auth+TLS+CSRF, serves the SPA
     │     ├─ modem via /dev/smd11 (AT): signal, SIM, SMS, call, APN
     │     ├─ direct reads: WiFi(hostapd.conf), clients(leases+arp), traffic(/proc/net/dev)
     │     └─ stock :80 proxy = fallback only (allow-listed; blocks 2000/2001/3000)
     ├─ tailscale / adguardhome  (installed; dormant until WAN + config)
     └─ (audio bridge off; needs tinymix spike + a registered call)
```
All services **auto-start on boot** via `/system/etc/init.qcom.post_boot.sh` → `/data/webui/boot-hook.sh` → `harden.sh` + `/data/linux/start-services.sh` (reboot-verified).

## 4. Access
- **Cockpit:** `https://192.168.100.1/` (self-signed cert) — admin login (creds out-of-band).
- **SSH root:** `ssh -o PubkeyAuthentication=no root@192.168.100.1` (creds out-of-band).
- **USB ADB root:** always available over the USB cable (the ultimate lifeline).
- Reachable on the WiFi/USB LAN only; cellular (`rmnet0`) inbound is firewalled off.

## 5. Status
| Area | State |
|------|-------|
| Firmware dump | ✅ complete (`dump/`) |
| Security audit | ✅ complete (2 passes) |
| Hardening | ✅ applied + persistent (remote ADB closed, WAN-inbound dropped, phone-home blocked, WiFi pw rotated) |
| Backdoor decommission | ✅ `:80`/`:8088` LAN-unreachable, OTA blocked (app not deleted) |
| Cockpit API + dashboard | ✅ deployed, authenticated, boots automatically |
| Direct (no-:80) reads | ✅ WiFi/clients/traffic/SIM/signal/device |
| SMS / calls | ⏳ code done (+ **fast-fail** when unregistered); **signal-gated** (modem must register — needs better signal/antenna) |
| LTE band-lock | ⚠️ feature built (API + Network-tab UI) but **this modem's firmware has NO band-lock AT command** (probed live — `$QCBANDPREF` is 2G/3G-only, `!BAND`/`^SETBAND`/`^SYSCFGEX`/`$QCRAT` ERROR, `+CLAC` has no LTE-band verb). ⇒ card is **display-only** (shows serving band + usable flag); true band-lock would need NV 65633/QMI. B20 is a hardware limit regardless |
| Cellular data | ⏳ attaches but unusable at current location — **band mismatch** (B20 only); needs B1/B3 or antenna |
| Tailscale / AdGuard | ⏳ installed, dormant (need working WAN + config) |
| Voice audio bridge | ⏳ off; needs one-time tinymix spike during a registered call |
| Delete the stock app | ⏳ pending host-side plumbing-takeover test (`cockpit/api/deploy/uz-netservices.sh`) |

## 6. Repo layout
- [`README.md`](README.md) — chronological investigation log.
- [`SPECS.md`](SPECS.md) — full hardware specs.
- [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) · [`EXPLOITS.md`](EXPLOITS.md) · [`IP-INVESTIGATION.md`](IP-INVESTIGATION.md) — security.
- [`CAPABILITIES.md`](CAPABILITIES.md) · [`ANTENNA.md`](ANTENNA.md) — bands/carriers + antenna guidance.
- [`LINUX-OPTIONS.md`](LINUX-OPTIONS.md) · [`CHROOT-LINUX-PLAN.md`](CHROOT-LINUX-PLAN.md) · [`COCKPIT-API-PLAN.md`](COCKPIT-API-PLAN.md) — plans.
- `cockpit/api/` — Python-stdlib HTTPS API (modem AT, SMS/call, auth/TLS, direct platform impls, stock proxy).
- `cockpit/web/` — vanilla SPA dashboard.
- `alpine/` — chroot scaffolding (`enter.sh`, `start-services.sh`, `pc-apk-proxy.py`).
- `webui/` — original PC-proxy UI + device scripts (`deploy/harden.sh`, `boot-hook.sh`, `start-webui.sh`); the `:8088` UI is decommissioned, scripts remain for harden/boot.
- `dump/` — firmware dump text records (`.img` blobs gitignored; kept locally as the EDL restore set).

## 7. Safety / lifelines
- **USB-ADB root** is independent of everything above and is the always-on recovery path.
- **SSH** into Alpine is a second root path, independent of the stock app.
- Firewall is **runtime-applied at boot**; `unharden.sh` reverts it.
- The stock app is **disabled-in-effect, not deleted** — fully reversible.
- Full EDL restore images in `dump/` (local).

## 8. Next steps (when ready)
1. **Signal/antenna** — directional LTE antenna on B1/B3 → SMS/calls/data become usable.
2. **Delete the stock app** — test `uz-netservices.sh` host-side (hostapd/dnsmasq/NAT/data takeover) with ADB lifeline, then `pm disable` + reboot-test, then remove the APK.
3. **Tailscale** auth + **AdGuard** config (once WAN works).
4. **Voice audio bridge** tinymix spike during a registered call.
