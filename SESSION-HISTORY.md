# Session History

A human-readable log of working sessions with Claude Code (what was asked, what was
done/found). Newest first. This is a reconstructed summary of each session, not a verbatim
transcript. For the canonical state see [`PROGRESS.md`](PROGRESS.md); for the terse dated
log see the Status Log in [`README.md`](README.md).

---

## Session 2026-11-07 → 2026-11-09 — SMS fast-fail, band-lock feature, live signal diagnosis

**Context at start:** clean checkpoint; the one blocker was the cellular B20 band mismatch
(hardware). Device healthy over USB-ADB (`ro.secure=0`, `br0` up).

### SMS fast-fail (shipped, commit `f123b3a`)
- Problem: when the modem isn't registered (the signal-gated state), an SMS send crawled
  through the whole CMGS prompt protocol and only returned a cryptic `+CMS ERROR: 500` ~30s
  later.
- Fix in `cockpit/api/modem.py`: `sms_send()` does a registration pre-check and rejects
  immediately with a human reason; `_send_pdu()` aborts with ESC (0x1B) if the `>` prompt
  never arrives instead of writing the payload and blocking 30s. API returns **409** for
  not-registered (vs 502 for a real modem error).

### LTE band-lock feature (shipped, commits `f123b3a`, `bc249ed`)
- Built end-to-end: `GET/POST /api/band`, a "Band lock" card in the Network tab, and a
  Qualcomm LTE band bitmask helper (E-UTRA band N → bit N-1) in `modem.py`.
- **Then probed the live modem** (`modem_at` + `AT+CLAC`). Verdict: **this firmware
  (`UZ801_V3.0_21_V01R01B10`, Longsung/Huawei `^SYSCONFIG`/`^PREFMODE`/`$QCNSP` dialect)
  exposes NO per-LTE-band lock over AT.** `$QCBANDPREF` here is 2G/3G/WCDMA-only (enum ends
  at `46.Any`); `AT!BAND`/`AT^SETBAND`/`AT^SYSCFGEX`/`$QCRAT` all ERROR; the full 32-command
  `+CLAC` surface has no LTE-band verb (only RAT/mode).
- So the card is **display-only** on this unit: GET shows the live serving band + a
  usable/unusable flag + visible cells; POST is a no-op (`platform.band_at` empty → 501).
  Scaffolding kept for any future modem that does expose the AT verb. A real band-lock here
  would need NV 65633 (`lte_bc_config`) via QMI/diag.

### User preferences recorded (memory)
- **Stop using the old `funcNo xxxx` naming** for cockpit features — the stock backdoor API
  isn't our primary path. Keep funcNo only in `stockproxy.py` (literal fallback protocol)
  and the forensic audit docs.
- **WiFi AP drops** — owner observed the hotspot dropping; deferred a performance/stress
  test to diagnose ("not now").

### Live signal diagnosis on the balcony (commit `cab6523` + the band-monitor tool)
- Owner moved the laptop+dongle to the balcony; signal became intermittent ("now it has,
  now it doesn't"). Watched the modem live.
- **Found:** the modem oscillates — registers with strong signal, then total loss (searching,
  CSQ 99), repeatedly. It keeps camping on **B7 (2600 MHz, EARFCN 3350)** whose RSRP swings
  −82 ↔ −140 dBm within seconds. B7 is unusable on this hardware (same bucket as B20).
- Fixed a real bug in `modem.py`'s EARFCN→band map: **B7 was missing** and `3100→B40` was
  wrong (3100 is B7; B40 is TDD 38650–39649). Added the B7 range so the cockpit labels it.
- Built a read-only live **band monitor** (`scratchpad/band-monitor.sh`): polls CEREG/CSQ/
  `$QCRSRP?`, decodes EARFCN→band, flags usable/not, logs to a file. Improved it to ignore
  sentinel RSRP values (0, −140, −44/B20-cap) and report the best **usable-band** cell.
- **Key result from a 3.5-min balcony sweep:** usable signal IS present — **B1 (2100) hits
  −49 to −66 dBm**, B8 (900) occasionally — but the modem won't *stay* on B1; it keeps
  hopping to the (raw-stronger but unusable) B20/B7, and can't be told to prefer B1.

### Connection reality + options discussed
- The "USB drops" were explained by the owner: the dongle was moved to an **external battery
  on the balcony** (not plugged into the laptop), so `adb` absence is expected, not a fault.
  Live access then needs either replugging USB or joining the laptop to the dongle's WiFi
  (`4G-UFI-XXX`) to reach cockpit/SSH at `192.168.100.1` (laptop was on home WiFi instead).
- **What can we do (band-hopping + no AT band-lock):**
  1. **Directional B1 antenna** (recommended) — board has a single **u.FL/IPEX** connector
     (plug-in, no soldering; see `ANTENNA.md`). Boosting B1 makes it the strongest cell so
     the modem camps on it. High confidence since B1 is already strong here.
  2. **Band-lock via the diag port** — edit the LTE band mask NV/EFS item over `diag` (root
     available) to allow only B1/B3/B8. Software equivalent of the antenna's selectivity;
     needs a diag NV tool + care (backups exist in `dump/`); requires USB (diag is over USB).
  3. **Positioning** — hold the dongle still ~15–20 s per spot to find where B1 stays strong
     and steady (the monitor is ready for this).

### Also this session
- Owner added PC-side routing helpers under `host/` (`setup-host-routing.sh` + `README.md`):
  pin the dongle iface to name `uz801` by USB ID and make it the lowest-priority default
  gateway (metric 2000), so plugging it in doesn't hijack the PC's default route.

### Continuity wrap-up
- Committed the owner's `host/` routing helper (`282d54f`) and this `SESSION-HISTORY.md`
  (`622c017`); all work pushed to `origin/master`, tree clean.
- Owner asked whether Claude has a "history" skill to preserve the chat. Answer: **no** — no
  such skill is available in this session; continuity is manual (memory files + the README
  Status Log + `PROGRESS.md` + this file). `SESSION-HISTORY.md` is a reconstructed summary,
  not a verbatim transcript (true verbatim would come from Claude Code's own on-disk session
  transcript, not from Claude rewriting it).
- Open thread: owner mentioned a specific skill name for history to check next time.
