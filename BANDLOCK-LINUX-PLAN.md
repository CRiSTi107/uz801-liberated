# Execution Plan — On-device (Linux) LTE band-lock

**Goal:** lock the modem to the usable LTE bands **B1/B3/B5/B8/B40** and disable
**B7/B20** (and others), entirely **on the dongle over Linux** (no Windows/QPST), so it
stops hopping to the strong-but-unusable B20/B7 and stays registered on B1/B3.

> **This is advanced and carries real brick risk** (modem NV/EFS editing). It is gated
> behind explicit go + backups. USB-ADB root is the lifeline throughout; the full
> `dump/` (incl. modem/EFS partitions) is the ultimate restore. Never touch IMEI (NV 550)
> or any RF-calibration item.

## PROGRESS (2026-11-09)
- **Phase 0 ✅ DONE** — EFS/modem-state partitions backed up to `diag/efs-backup/` (local,
  gitignored): `fsc`(p16,1K), `fsg`(p20,1.5M), `modemst1`(p13,1.5M), `modemst2`(p14,1.5M) +
  md5s; the full `dump/` is the deeper restore.
- **Phase 1 ✅ PROVEN** — `diag/qcdiag_probe.py` talks to `/dev/diag` from the Alpine chroot
  and got a valid **Extended Build ID** reply from the modem. Key device facts:
  `DIAG_IOCTL_SWITCH_LOGGING`=7 needs the **struct arg** `pack('<iiii',MEMORY_DEVICE_MODE,0,-1,0)`
  (int form → EFAULT); write = `pack('<I',0x20)` + HDLC(payload); HDLC FCS = reflected 0x8408.
  `/dev/diag` must be bind-mounted into the chroot (done live; add to `enter.sh` when we commit to it).
- **Phase 2 ❌ DEAD END — Route B not feasible on this modem (2026-11-09).**
  `diag/qcdiag_efs.py` implements EFS2 (SUBSYS 0x4B / subsys 0x13) + legacy NV_READ (0x26) over
  the proven transport. The transport is 100% working, but the modem's diag is **locked down to
  info-only**:
  | Command class | Example | Responds? |
  |---|---|---|
  | Version / build info | `0x00`, `0x7c` | ✅ |
  | EFS2 capability | HELLO (0), QUERY (1) | ✅ (QUERY → `4b 13 01 00 03…` v3+limits) |
  | EFS2 **file I/O** | STAT(15), STATFS(19), OPEN(2), READ(4) | ❌ **silent (0 bytes)** |
  | **Legacy NV** | NV_READ `0x26` (items 10/71/85/441/946/2954) | ❌ **silent** |
  | SPC unlock | `DIAG_SPC_F` 0x25 (`000000`/`00000000`) | ❌ **silent** |

  Silence on `STATFS /` (root always exists) and on every NV_READ is **not** "file not found"
  (that returns an errno) and **not** an SPC lock (SPC 0x25 itself is dropped) — it is a
  **restricted commercial diag build with NV/EFS data access compiled out / disabled**. Only the
  capability/logging subset is live. **No key opens this**, so Route B (DIAG NV/EFS band write)
  cannot be done on this device. Nothing was written to the modem; this stayed read-only throughout.
- **Remaining options (both away from DIAG):**
  - **Route A — QMI runtime band-pref via `qmuxd`** (reversible, re-applied each boot). Still
    unexplored; needs a small QMUX/QMI client to `/dev/socket/qmux_*` (no FOSS client targets
    qmuxd). High effort, uncertain.
  - **Fall back to the RF fix** — directional B1/B3 antenna (+ optional 800 MHz notch), the
    hardware path in `ANTENNA.md`/`CAPABILITIES.md`. This was already the recommended fix, and
    earlier live data suggested the real blocker is intermittent **EMM attach failure**, not band
    choice — so band-lock likely wouldn't have fixed it even if Route B had worked.

## ✅ ROUTE A IS LIVE (2026-11-09) — QMI over a free SMD channel works
After Route B (DIAG) proved a dead end, Route A succeeded:
- **Transport:** qmuxd holds `/dev/smdcntl0..7`; **`smdcntl8` (and `9`) are root-owned and
  unused by qmuxd**. Opening `smdcntl8` and speaking **standard QMUX/QMI** gives a parallel QMI
  control point (modem sends a QMI_CTL SYNC indication 0x0027 on connect). Tools:
  `diag/qmi_probe.py` (channel finder), `diag/qmi_bandpref.py` (read), `diag/qmi_bandlock.py`
  (set, dry-run default). The chroot needs the node: `mknod /dev/smdcntl8 c 244 8`.
- **READ (qmi_bandpref.py):** allocated NAS client, QMI_NAS Get System Selection Preference
  (0x0034) →
  - `mode_pref = 0x0010` (LTE-only already),
  - **`lte_band_pref = 0x000007ff5bdf3fff`** = **all of B1–B43 enabled** (why it roams onto B20/B7),
  - Get RF Band Info (0x0031) → serving **EARFCN 6425 = Band 20 (800 MHz, unusable)** — caught it
    camped on B20, the exact failure.
- **PLAN the write:** QMI_NAS **Set System Selection Preference (0x0033)**, TLV 0x14
  `lte_band_pref = 0x0000008000000095` = **{B1,B3,B5,B8,B40}**, TLV 0x17 `change_duration = 0x00`
  (**POWER_CYCLE = runtime, reverts on reboot, NO NV write, zero brick risk**). Re-apply each boot
  from the boot-hook. Dry-run SET frame (verified bytes):
  `011b0000030200010033000f00140800950000008000000017010000`.
- **Status:** read done; **the --apply SET is gated on explicit owner go** (per "stop before any
  WRITE" — though this write is runtime, not NV). After apply: re-read + Get RF Band Info to
  confirm the modem drops B20 and re-camps on B1/B3.

## Why this is non-trivial (recap)
- **No AT band-lock verb** on this modem (confirmed: `$QCBANDPREF` is 2G/3G-only; `!BAND`/
  `^SETBAND`/`^SYSCFGEX`/`$QCRAT` all ERROR).
- **No userspace QMI node** (`libqmi`/`qmicli` need `cdc-wdm`/QRTR; this is SMD+`qmuxd`).
- ⇒ the only on-device door is **`/dev/diag`** (present, char 245,0) speaking the Qualcomm
  **DIAG** protocol, OR a QMI client that talks to **`qmuxd`**'s socket.

## Two routes (A = safer/temporary, B = persistent)
### Route A — QMI runtime band preference (SAFER, non-persistent → re-apply at boot)
Send QMI **NAS "Set System Selection Preference"** with an `lte_band_pref` bitmask. This is
a **runtime** setting: no NV write, reverts on reboot (so **no brick risk**), re-applied
each boot from our boot-hook.
- **Blocker:** reaching QMI. `qmuxd` owns the QMI control channels (`/dev/smdcntl*`). Options
  to investigate: (a) connect to qmuxd's QMUX socket (`/dev/socket/qmux_*`) and speak QMUX+QMI;
  (b) find a free `smdcntl` and speak raw QMI. Both need a small QMUX/QMI client (no FOSS one
  targets qmuxd directly). **Investigation step decides if A is feasible before touching NV.**

### Route B — DIAG write of the band NV/EFS (PERSISTENT, riskier)
Write the LTE band item via DIAG: **NV 65633 `lte_bc_config`** (an EFS item;
`/nv/item_files/modem/mmode/lte_bc_config` or `lte_bandpref`). Persistent across reboots.
This is the "real" band-lock but a bad EFS write can corrupt modem config.

## Tooling (build in the Alpine chroot; python3 already installed)
- Base the DIAG transport on **QCSuper** (P1sec/QCSuper, MIT, mostly pure-Python) — it already
  implements `/dev/diag` access (the `DIAG_IOCTL_SWITCH_LOGGING` ioctl + HDLC 0x7E framing +
  CRC16). We reuse its transport and **add an EFS2 read/write** for the band item.
- DIAG essentials to implement/verify: HDLC frame (escape 0x7D/0x7E, append CRC-16-CCITT,
  terminate 0x7E); EFS2 subsystem (cmd 0x4B, subsys 0x13) ops: `HELLO`, `OPEN`, `READ`,
  `WRITE`, `CLOSE` — or NV_READ/NV_WRITE (0x26/0x27) if the item is legacy NV.
- Band mask (bit N-1 = E-UTRA band N): keep **B1(bit0) B3(bit2) B5(bit4) B8(bit7) B40(bit39)**;
  clear **B7(bit6) B20(bit19)** and the rest. (Little-endian 8-byte mask ⇒ byte0=0x95, byte4=0x80,
  rest 0x00 — **verify against the item's actual format after the READ**, do not assume.)

## Phases
0. **Pre-flight / backup (do all):**
   - Confirm USB-ADB root + SSH lifelines.
   - `apk add python3 py3-pip` (have it); fetch QCSuper into the chroot (via the PC proxy or
     a pushed tarball — it's small, pure-Python + a couple deps).
   - **Dump the modem EFS partitions now** for a guaranteed restore: `adb pull` of
     `by-name/{modemst1,modemst2,fsg,fsc}` (small) → keep alongside `dump/`. (The firmware
     `dump/` already has `modem.img`; add the live EFS state.)
1. **DIAG bring-up:** get QCSuper (or our minimal client) talking to `/dev/diag` — prove it by
   reading a benign item (e.g. EFS `HELLO`, or the software version via DIAG).
2. **READ the band item (backup the exact bytes):** EFS read `lte_bc_config` (try the known
   paths / NV 65633). Save the raw value. **This also reveals the real format** so the new
   mask is computed correctly (not assumed).
3. **Decide A vs B:** if Route A (QMI/qmuxd) proved feasible in step 1's sibling investigation,
   prefer it (reversible). Otherwise Route B (EFS write).
4. **WRITE the new band mask** (B1/B3/B5/B8/B40). Single item, exact length as read back.
5. **Apply:** `modem_at "AT+CFUN=1,1"` (modem reset) — avoid a full device reboot (WiFi wedge).
6. **VERIFY:** `diag/signal-monitor.sh` — the modem must now only ever show B1/B3/B5/B8/B40 and
   never B20/B7; registration should become stable.
7. **Rollback if wrong:** EFS-write the saved bytes back (step 2), or restore the EFS partitions
   dumped in step 0, or full `dump/` restore via EDL.

## Risk table
| Step | Risk | Mitigation |
|------|------|------------|
| DIAG EFS write wrong item/len | modem no-service / brick | read-first exact bytes; only the band item; EFS partition backup |
| CFUN reset | transient drop | expected; re-registers |
| Mask format wrong | bad band set | compute from the READ value, verify, keep backup |
| Tooling bug | corrupt write | dry-run reads first; small, reviewed writer |

## Deliverables (when executed)
- `diag/qcdiag.py` (or vendored QCSuper + `diag/bandlock.py`) — the DIAG client + band read/write.
- `diag/bandlock-backup/` — the pre-change band bytes + EFS partition dumps.
- Boot-hook line (Route A only) to re-apply the runtime band pref each boot.
- Logged in README / PROGRESS / SESSION-HISTORY.

## Go/no-go
Proceed phase-by-phase; **stop and show the READ value before any WRITE.** If DIAG bring-up
or the EFS read can't be made reliable, fall back to the RF route (directional antenna + 800 MHz
notch filter) or Windows QPST — band-lock is an optimization, not required for the dongle to work.
