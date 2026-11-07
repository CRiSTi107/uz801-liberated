# Plan — Secure API + "Cockpit" dashboard for the UZ801

**Goal:** replace the insecure, unauthenticated stock `/ajax` with a **new authenticated
API + a modern web "cockpit"**, running in the Alpine chroot (`CHROOT-LINUX-PLAN.md`),
adding **SMS** and **call-signaling** on top of the existing status/WiFi/APN controls.

**Status: PLAN ONLY.** Nothing is built until you approve. At build time I'll use
**parallel agents** (one for the API/modem layer, one for the cockpit SPA) — planning is
done inline here because it's context-heavy.

---

## Why not modify the stock Java servlet
The insecure API is `com.mifiservice.server.AjaxSevlet` compiled into `MifiService.apk`
(+ `ajax.js`). Editing it means decompiling/recompiling/re-signing the APK — fragile and
risky. **We leave it alone** and put a new service *in front of / beside* it.

## Tech choice
- **Language: Python 3** in the Alpine chroot. Reasons: already in the stack; ideal for
  AT/serial + SMS **PDU** handling; **pure-Python Flask** avoids musl-wheel pain.
  (Avoid FastAPI/pydantic-v2 — Rust core, awkward on musl. Go is the alt: one static
  binary cross-compiled on the PC, if we want that instead.)
- **Web framework: Flask** (pure-Python) + **`waitress`** (pure-Python WSGI server), or
  Python stdlib `http.server` if we want zero deps. **TLS** via stdlib `ssl`
  (self-signed cert generated on first run).
- **Store: SQLite** (stdlib `sqlite3`) for SMS, call log, sessions, settings.
- **Modem access:** read/write **`/dev/smd11`** directly (0666, bind-mounted into the
  chroot) using the same AT interface `modem_at` uses. A small serializer wraps it so only
  one AT transaction runs at a time.

## Architecture
```
Browser (LAN / Tailscale)
      │  HTTPS + auth (token/session)
      ▼
 cockpit-api  (Python, Alpine chroot, :443)
      ├── /dev/smd11  ── AT ──► modem        (status, signal, SMS, call, APN)
      ├── http://127.0.0.1:80/ajax (allow-list)► stock MifiService  (WiFi, LAN, clients)
      └── SQLite                                   (SMS, call log, sessions, settings)
```
- Binds **`:443` (HTTPS)**; reachable on the LAN and over **Tailscale** (remote).
- **Does NOT expose** the dangerous stock funcNos — the proxy allow-list blocks
  `2000` (EDL), `2001` (ADB enable), `2002` (IMEI write — gated behind an explicit
  "danger" toggle), `3000` (flash). Everything privileged requires login.

## Security model (fixes the audit findings for the new surface)
- **Auth:** username + password (argon2/pbkdf2 via stdlib `hashlib`), signed session
  cookies, optional API token for scripts. First-run sets the admin password.
- **TLS:** self-signed cert (stdlib), HSTS, secure cookies.
- **CSRF** token on state-changing calls; rate-limit login.
- **funcNo allow-list** for the stock proxy (read-only funcs open to logged-in users;
  destructive ones blocked or behind a confirm+danger flag).
- Still behind the device firewall: `rmnet0` inbound dropped (no internet exposure),
  LAN reachable (per your policy), remote only via Tailscale. The old unauth `:80`
  remains (stock app) — documented residual; the cockpit is the thing *you* use.

## Cockpit dashboard (tabs)
| Tab | Shows / does | Source |
|-----|--------------|--------|
| **Overview** | signal (RSRP/RSRQ/band/EARFCN), operator, reg state, data up/down, uptime, link health (live ping) | AT `$QCRSRP?`,`+CSQ`,`+CEREG?`,`+COPS?` |
| **SMS** | inbox / sent / **compose+send**, delete, new-message notify | AT `+CMGF/+CMGL/+CMGR/+CMGS/+CNMI/+CMGD`, SQLite |
| **Calls** | dial `ATD` / answer `ATA` / hang up `ATH` / status `+CLCC`, **plus an optional 2-way audio bridge over IP** (stretch — see below) | AT + tinyalsa + bridge |
| **SIM / Identity** | IMEI (read/**write** behind danger toggle), IMSI, ICCID, SIM status | AT `+CGSN/+CIMI`, funcNo 1029/1015/2002 |
| **Network / APN** | registration, band info, APN view/set, net-mode | AT `+CGDCONT/+COPS`, funcNo 1005/1016-1018 |
| **WiFi** | SSID/password/encryption/clients (proxied, allow-listed) | funcNo 1006/1007/1009/1010/1011 |
| **System** | firewall status, Tailscale status, AdGuard status, reboot, service controls | iptables, `tailscale status`, scripts |
| **Settings** | admin password, API tokens, TLS cert, auto-refresh | SQLite |

## Modem capability facts (verified 2026-11-07)
- **SMS: supported** — `+CMGF:(0-1)`, `+CSMS:0,1,1,1`, storage `+CPMS` ME/SM. Implement send/receive.
- **Calls: signaling works, and audio is software-tappable** — `+CLCC`/`+CPAS` work, IMS
  daemons present. There's **no physical codec/mic/speaker**, BUT the DSP/driver exposes the
  voice audio path in software (see "Voice audio bridge" below), so a bridge over IP is
  feasible. (`+CMUT?`→ERROR just means the *handset* mute control is absent, not the path.)
- **USB: gadget only** (`msm_hsusb`) — no host/OTG → no peripherals, no storage expansion
  (so audio must go over IP, not a USB sound card).
- **APN:** auto-resolves correctly (Digi→`internet`); **no DB update needed**. (If ever
  needed: edit `/system/etc/apns-conf.xml` — a `/system` change — or set via AT.)

## Voice audio bridge over IP (the "virtual mic/speaker") — feasible, ambitious
**Finding (verified 2026-11-07):** even with no codec, the audio path is fully present in
software — ALSA card `msm8x16sndcardm`, **tinyalsa** tools (`tinycap/tinyplay/tinymix`),
`mixer_paths.xml`+`acdbdata`, and the right PCM endpoints:
- **Voice Downlink Capture** (what the far end says) + **Voice Farend Playback** (inject
  audio *to* the far end = the "virtual mic"), plus **AFE-PROXY RX/TX** (generic software
  audio in/out to the DSP). `tinymix` shows 595 controls incl `PROXY_RX` routing + Voice gain/mute.

**How the bridge would work:**
```
far end ⇄ modem/DSP ⇄ (tinymix routes voice ⇄ AFE-PROXY / Farend+Downlink PCM)
                         │ tinycap (downlink)            ▲ tinyplay (uplink)
                         ▼                               │
                   cockpit-api  ── WebSocket/PCM ──► browser (cockpit) OR a client app
                                                     plays downlink on speaker,
                                                     captures mic → back to uplink
```
- **Transport:** cleanest is **WebSocket PCM straight into the cockpit browser** (Web Audio
  API plays downlink, `getUserMedia` mic → uplink) — so you "use the phone" from the cockpit
  tab on a device that *has* a mic/speaker. Raw PCM over TCP/UDP to a native client also works.
- **Why it's a stretch, not a given:** (1) it needs the right **`tinymix` routing** to connect
  the live voice session to the proxy/farend ports (config exists, but the exact control set
  must be worked out); (2) voice must actually **establish** (CS via CSFB, or VoLTE/IMS) on
  the network; (3) possible **ACDB calibration** quirks (muted/distorted audio) — `acdbdata`
  is present, which helps. Any of these can need iteration.
- **De-risking spike (cheap, do first):** during a test call, try routing + `tinycap` of
  Voice Downlink to a WAV and `tinyplay` a tone into Farend; if we hear/capture audio, the
  full bridge is a straightforward build. This proves/kills it in one short session.

## Provisioning script — `provision.sh` (one-shot onboarding for a fresh dongle)
Capstone orchestrator so a **brand-new UZ801** is brought to our full setup with one command.
(Proposed name `provision.sh`; alternatives: `onboard.sh`, `bootstrap-dongle.sh`.)
Idempotent stages, each skippable/re-runnable:
1. **Enable ADB** on a fresh unit: POST `funcNo 2001` to the stock web API (`/usbdebug.html`),
   wait for USB re-enumerate. *(Only step that uses the stock backdoor — bootstrap only.)*
2. **Verify root** over USB ADB; record device serial/MAC.
3. **Deploy** the current web UI (`webui/`) + `start-webui.sh`.
4. **Harden** (`harden.sh`): close 7628, drop `rmnet0` inbound, block phone-home; rotate WiFi pw.
5. **Install persistence** boot hook.
6. **Set up the Alpine chroot + stack** (`CHROOT-LINUX-PLAN.md`) and the **cockpit/API**.
7. **Verify** end-to-end (UI, firewall, SSH, Tailscale, API login) and print a summary.
8. *(Later / opt-in)* **Decommission** the stock backdoor UI once the new API is proven.
Runs from the PC (`provision.sh <adb-serial>`); self-contained with the repo's scripts.
Can be written **now** for stages 1–5 (they exist); 6–8 are filled in as those get built.

## Build phases (where agents come in)
1. **Scaffold** the Alpine chroot + stack (`CHROOT-LINUX-PLAN.md`).
2. **Agent A — API/modem layer:** Python service: AT serializer over `/dev/smd11`, SMS
   (PDU+text), call-signaling, stock-`/ajax` allow-list proxy, auth, TLS, SQLite.
3. **Agent B — cockpit SPA:** reuse the existing `webui/` design; add SMS/Calls/System
   tabs; talk to the new HTTPS API.
4. **Integrate + launch from `boot-hook.sh`**, verify end-to-end (login, signal, SMS, API), document.
5. **(Spike) Voice audio bridge** de-risk — `tinymix` route + `tinycap`/`tinyplay` during a
   test call; build the WebSocket PCM bridge only if the spike proves audio.
6. **Wire into `provision.sh`** so a fresh device reaches this state in one command.
7. **Decommission the stock backdoor — LAST, and gated.** Only after the new API + SSH are
   proven, remove/neutralise the stock unauth UI/`/ajax`. **Hard gate before touching it:**
   confirm we still have (a) **USB ADB root** and (b) **working SSH into the Alpine box**
   independent of the stock app — so we can never lose root/entry. Keep the stock stuff
   running until that's verified (per your instruction).

## Decisions — RESOLVED (2026-11-07)
1. **API port:** **`:443` HTTPS**. ✅
2. **Stock/backdoor + current `:8088` UI:** **KEEP for now**; decommission only in Phase 7,
   after the new API + SSH are proven and root/ssh is confirmed safe. ✅
3. **Voice audio bridge:** **pursue** (virtual mic/speaker over IP via AFE-proxy/Voice ports),
   starting with the de-risking spike. ✅
4. **Write/edit functions (incl. IMEI write):** **keep all**, behind auth + danger toggles. ✅

### Still to confirm at build time
- OK to **send one test SMS** during verification (costs a message)?
- OK to **test-dial a number you provide** for the voice/audio spike?
- Provisioning script name: **`provision.sh`** unless you prefer `onboard.sh` / `bootstrap-dongle.sh`.

Say the answers (or "defaults") and whether to **start building**, and I'll scaffold Alpine
then dispatch the two build agents.
