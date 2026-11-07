# UZ801 Cockpit (web SPA)

A modern, self-contained dashboard for managing a UZ801 4G USB dongle. It is the
**frontend** for the secure on-device API (built separately). This is the evolution
of `webui/` — same dark theme, tokens, card/tab layout, toast + confirm-dialog
patterns — rebuilt against an authenticated REST + WebSocket contract.

- **Vanilla HTML/CSS/JS only.** No build step, no bundler, no CDN, no framework.
  Everything is self-contained and works offline (all assets are local/inline).
- **Served by the API over HTTPS at the same origin.** All calls are same-origin.
- Works at phone width (responsive sidebar collapses to an icon bar).
- Accessible: semantic landmarks, `aria-live` regions, keyboard-friendly dial pad,
  visible focus rings, `prefers-reduced-motion` respected.

## Files

```
cockpit/web/
├── index.html        # app shell + all 8 tab views + auth screens + confirm dialog
├── css/styles.css    # dark theme (reuses webui tokens) + cockpit components
├── js/api.js         # centralized fetch() helper, session/CSRF, formatting helpers
├── js/audio.js       # audio bridge (WebSocket PCM <-> Web Audio) — AudioBridge class
├── js/app.js         # UI wiring, auth gate, navigation, polling, all REST actions
└── README.md         # this file
```

## Running / serving

The secure API must serve `index.html` and the `css/`, `js/` assets at the web root,
and expose the API under `/api` and the audio socket at `/ws/audio` **on the same
origin**. No separate web server or PC-side proxy is needed. For local UI-only
preview (no live data): `python3 -m http.server` from this directory — every API
call will fail gracefully and the offline banner will show.

---

## REST contract consumed by this SPA

The API agent must implement this exactly. All request/response bodies are JSON.
Cookies are session-based (`credentials: "same-origin"`). Every **state-changing**
request (POST/PUT/DELETE) sends header **`X-CSRF: <csrf>`** using the token from
`GET /api/session`. Any `401` anywhere returns the UI to the login screen.
`502/503/504` are treated as "backend/device unreachable" (offline banner).

### Auth / session

| Method & path | Body | Response |
|---|---|---|
| `GET /api/session` | — | `{ first_run: bool, authenticated: bool, csrf: string, user: string\|null }` |
| `POST /api/setup` | `{ username, password }` | first-run only; logs in. `{ csrf, user }` |
| `POST /api/login` | `{ username, password }` | `{ csrf, user }` (401 on bad creds) |
| `POST /api/logout` | `{}` | `{}` |

Boot logic: `GET /api/session` → if `first_run` show **setup** screen; else if not
`authenticated` show **login**; else show the app. `setup`/`login` return a fresh
`csrf` which the SPA then uses. (If your framework rotates CSRF on login, return the
new token in the response as shown.)

### Overview — `GET /api/status` (polled on the auto-refresh interval)

```jsonc
{
  "signal":       { "rsrp": -95, "rsrq": -10, "sinr": 7, "csq": 18 },
  "radio":        { "band": "B3", "earfcn": 1850, "pci": 123, "cell_id": "0x1A2B3C",
                    "band_usable": true, "band_hint": "optional string" },
  "registration": { "state": "registered", "registered": true,
                    "operator": "Digi", "rat": "LTE" },
  "data":         { "connected": true, "ip": "10.1.2.3",
                    "up_bytes": 12345, "down_bytes": 67890 },
  "uptime_s":     3600,
  "link":         { "ping_ms": 42, "up": true, "target": "1.1.1.1" }
}
```

- `signal.rsrp` (dBm) drives the bars + meter; `csq` is shown with an approximate dBm.
- **Band-20 mismatch handling:** if `radio.band_usable === false`, the Overview shows
  an amber hint (using `radio.band_hint` if provided, else a generic message). Set this
  flag whenever the camped band is known-unusable on this hardware.
- `link` powers the live "link health" indicator. `up:false` or missing `ping_ms`
  renders "link down"/"unknown". A standalone `GET /api/ping` is **not** required —
  fold ping results into `/status.link`.

### SMS

| Method & path | Body | Response |
|---|---|---|
| `GET /api/sms?box=inbox` or `?box=sent` | — | `{ messages: [ { id, address, text, ts, unread } ] }` |
| `POST /api/sms` | `{ to, text }` | `{ id }` (queues/sends the message) |
| `DELETE /api/sms/{id}` | — | `{}` |

- `ts` may be Unix seconds or ISO-8601; `address` is the peer number (from/to).
- `unread:true` on inbox items drives the sidebar badge and a "New SMS" toast.
- The compose box computes GSM-7 vs UCS-2 and segment count **client-side** (display
  only); the API does the real encoding/splitting.

### Calls

| Method & path | Body | Response |
|---|---|---|
| `GET /api/call` | — | `{ calls: [ { id, number, direction, state, duration } ] }` |
| `POST /api/call` | `{ number }` | dial; `{ id }` |
| `POST /api/call/answer` | `{}` | answer the incoming call |
| `POST /api/call/hangup` | `{}` | hang up the active/incoming call |

- `state` ∈ `dialing | alerting | active | incoming | waiting | held` (CLCC-style).
  `incoming`/`waiting` enables the **Answer** button; `active` starts the call timer
  (seeded from `duration` seconds). Empty `calls` array = idle.
- `direction` ∈ `incoming | outgoing` (optional, shown as a label).

### Audio bridge (beta) — WebSocket `/ws/audio`

Same-origin WS (`wss:` under HTTPS). Contract:

- **Binary frames = raw PCM, signed 16-bit little-endian (`S16_LE`), mono.** Sample
  rate is negotiated via a control message; **default 8000 Hz** if none is sent.
- **Text frames = JSON control messages:**
  - server → client: `{"type":"start","rate":8000,"format":"S16_LE","channels":1}`,
    `{"type":"status","state":"...","message":"..."}`, `{"type":"rate","rate":16000}`,
    `{"type":"stop"}`
  - client → server: `{"type":"start"}` (sent on open to request audio),
    `{"type":"stop"}` (sent on disconnect)
- **Downlink** (far-end audio) binary frames → played via Web Audio (`AudioContext`,
  scheduled gap-free; the context resamples from the PCM rate).
- **Uplink** ("virtual mic"): browser mic via `getUserMedia` → downsampled to the
  negotiated rate → `S16_LE` mono binary frames sent back on the same socket.
- Fully optional & beta: if the WS, `AudioContext` or `getUserMedia` are unavailable,
  the panel reports the reason and the rest of the Calls tab keeps working.

### SIM / Identity

| Method & path | Body | Response |
|---|---|---|
| `GET /api/sim` | — | `{ imei, imsi, iccid, sim_status, msisdn }` |
| `POST /api/sim/imei` | `{ imei, confirm: true }` | writes modem IMEI (danger-gated) |

- Client validates 15 digits + shows a Luhn check; the write is behind a prominent
  danger confirm and sends `confirm:true`. The API must also enforce the danger gate.

### Network / APN

| Method & path | Body | Response |
|---|---|---|
| `GET /api/network/apn` | — | `{ apn, user, pwd, auth }` |
| `POST /api/network/apn` | `{ apn, user, pwd, auth }` | save profile |
| `POST /api/network/netmode` | `{ mode }` | `mode` ∈ `auto \| 4g \| 3g \| 2g` |
| `GET /api/band` | — | `{ supported[], serving{band,usable,hint}, cells[], configured, current }` |
| `POST /api/band` | `{ bands }` | `bands` = `auto` \| a label (`B3`) \| a list (`["B1","B3"]`); 501 if `platform.band_at` unset |

Registration/band info for this tab comes from `GET /api/status` (mirrored).

### WiFi

| Method & path | Body | Response |
|---|---|---|
| `GET /api/wifi` | — | `{ ssid, password, encryption, max_clients }` |
| `POST /api/wifi` | `{ ssid, password, encryption, max_clients }` | save |
| `GET /api/wifi/clients` | — | `{ clients: [ { hostname, ip, mac } ] }` |

`encryption` ∈ `open \| wpa2 \| wpa2wpa3 \| wpa3`.

### System

| Method & path | Body | Response |
|---|---|---|
| `GET /api/system` | — | see below |
| `POST /api/system/service` | `{ name, action }` | `action` ∈ `start \| stop \| restart` |
| `POST /api/system/reboot` | `{}` | reboot the device |

```jsonc
{
  "firewall":  { "enabled": true,  "summary": "rmnet0 inbound dropped" },
  "tailscale": { "running": true,  "state": "up", "ip": "100.x.y.z", "hostname": "dongle" },
  "adguard":   { "running": false, "summary": "off" },
  "services":  [ { "name": "cockpit-api", "status": "running", "description": "..." } ]
}
```

A service `status` of `"running"` (or `running:true`) renders a Stop button; anything
else renders Start.

### Settings

| Method & path | Body | Response |
|---|---|---|
| `POST /api/settings/password` | `{ current, new }` | change admin password |
| `GET /api/tokens` | — | `{ tokens: [ { id, name, created, last_used } ] }` |
| `POST /api/tokens` | `{ name }` | `{ id, token }` — `token` shown **once** |
| `DELETE /api/tokens/{id}` | — | revoke |

Auto-refresh interval is a client-only preference (`localStorage`, `cockpit.interval`).

---

## Assumptions the backend must honor

1. **Same origin.** The API serves this SPA and exposes `/api/*` and `/ws/audio` on the
   same host/port. No CORS is configured for.
2. **Session cookie auth** with a **CSRF token** returned by `GET /api/session` (and by
   `login`/`setup`), echoed back in the `X-CSRF` header on every write. Cookies should be
   `HttpOnly; Secure; SameSite=Strict`.
3. **Status is one call.** Overview polls only `GET /api/status`; it must bundle signal,
   radio, registration, data, uptime and link/ping. Partial data is fine (missing fields
   render as `--`).
4. **`radio.band_usable:false`** is the signal for the Band-20-type mismatch hint.
5. **Error shape:** non-2xx responses should carry `{ "error": "message" }` (or
   `{ "message": "..." }`); the SPA surfaces it in a toast. `401` ⇒ re-login,
   `502/503/504` ⇒ offline banner.
6. **IDs are opaque strings** (SMS ids, token ids, call ids). The SPA never parses them.
7. **Audio PCM is `S16_LE` mono**; rate defaults to 8000 Hz unless a control message
   says otherwise. Server sends `{"type":"start",...}` on connect.
8. **Timestamps** accept Unix seconds or ISO-8601.
9. **Danger gating is enforced server-side too** — the client confirm dialogs and
   `confirm:true` on IMEI write are UX, not security.

## Notes / deviations from `webui/`

- The old UI spoke the unauthenticated stock `funcNo` protocol at `/ajax`. The cockpit
  speaks the new authenticated REST contract above; `js/api.js` is a clean rewrite
  (session + CSRF + 401 handling) but keeps the same single-helper philosophy and the
  same `ApiError`/offline model so the app code reads the same way.
- Design language (tokens, cards, grid, badges, toasts, confirm `<dialog>`, offline
  banner, responsive sidebar) is carried over verbatim from `webui/css/styles.css`,
  extended with cockpit-only components (dial pad, message list, VU meters, service
  rows, auth screens).
