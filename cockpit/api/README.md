# cockpit-api

A secure, HTTPS, authenticated management API for the **UZ801 4G USB dongle**
(Qualcomm MSM8916, Android 4.4.4). It replaces the dongle's *unauthenticated*
stock web API with a login-gated REST service that talks directly to the modem
over `/dev/smd11`, adds SMS and call signalling, and forwards only an
**allow-list** of stock `funcNo` calls for WiFi/LAN.

It is designed to run in the **Alpine armhf chroot** on the dongle
(`CHROOT-LINUX-PLAN.md`), using the **Python 3 standard library only** — no
third-party packages, nothing to compile, musl-safe.

> Status: backend only. A frontend "cockpit" SPA consumes the same contract.

---

## Table of contents
- [Quick start (offline / mock)](#quick-start-offline--mock)
- [Running on the device](#running-on-the-device)
- [Dependencies & musl-safety](#dependencies--musl-safety)
- [Configuration](#configuration)
- [Security model](#security-model)
- [REST API contract](#rest-api-contract)
- [Stock `/ajax` proxy allow-list](#stock-ajax-proxy-allow-list)
- [Audio bridge (WebSocket PCM)](#audio-bridge-websocket-pcm)
- [File map](#file-map)
- [Things to fill in on the device](#things-to-fill-in-on-the-device)

---

## Quick start (offline / mock)

Runs on any normal PC with Python 3 — **no hardware, no device**. The mock modem
fakes AT replies so every modem-backed endpoint works end to end.

```sh
cd cockpit/api
COCKPIT_MOCK=1 COCKPIT_ARGS="--no-tls --port 8443" ./run.sh
# equivalently:
python3 server.py --mock --no-tls --port 8443
```

Then, first run sets the admin password, logs in, and reads status:

```sh
BASE=http://127.0.0.1:8443
# first run: setup creates the admin AND logs in (sets cookie, returns csrf)
curl -s -c jar.txt -X POST $BASE/api/setup  -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"changeme123"}'
# or, once set up, log in:
curl -s -c jar.txt -X POST $BASE/api/login -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"changeme123"}'
curl -s -b jar.txt $BASE/api/session  # -> {authenticated:true, csrf, user, audio}
curl -s -b jar.txt $BASE/api/status   # signal/radio/registration/data/uptime_s/link
```

State-changing calls need the CSRF header, e.g. send an SMS:

```sh
CSRF=$(curl -s -b jar.txt $BASE/api/session | python3 -c 'import sys,json;print(json.load(sys.stdin)["csrf"])')
curl -s -b jar.txt -X POST $BASE/api/sms -H "X-CSRF: $CSRF" \
     -H 'Content-Type: application/json' -d '{"to":"+40740000000","text":"hi"}'
```

> `--no-tls` is **development only**. On the device always serve HTTPS.

---

## Running on the device

Inside the Alpine chroot:

```sh
apk add python3 openssl          # openssl is needed once for the TLS cert
# (optional) apk add tinyalsa    # only for the voice audio bridge

cd /data/linux/cockpit/api
cp config.example.json config.json   # then edit paths/values
./run.sh                              # serves HTTPS on 0.0.0.0:443
```

On first start with no cert present, `openssl` generates a self-signed
cert+key at the configured paths. Launch it from `boot-hook.sh` for autostart
(see `CHROOT-LINUX-PLAN.md`).

---

## Dependencies & musl-safety

**Zero Python dependencies.** Stdlib only: `http.server`, `ssl`, `sqlite3`,
`json`, `hashlib`, `hmac`, `base64`, `secrets`, `subprocess`, `threading`,
`urllib`, `socket`, `struct`, `select`.

This is deliberate for Alpine/musl/armhf: no C/Rust wheels to build. The banned
`cryptography` is avoided by shelling out to the **`openssl` CLI** for the
one-time self-signed cert, and the WebSocket server is implemented from scratch
over the stdlib socket (`wsframe.py`).

System packages (via `apk`, not pip):
- `python3` — runtime.
- `openssl` — first-run TLS cert generation. **Required.**
- `tinyalsa` — *optional*, only for the audio bridge (`tinycap`/`tinyplay`/`tinymix`).

Optional pure-Python WS libraries (`websockets`, `simple-websocket`) are listed
in `requirements.txt` but **not used** by default — both are pure-Python/musl-safe
if you ever prefer them over the built-in implementation.

---

## Configuration

All tunables live in `config.json` (copy from `config.example.json`). Any key
may be omitted — it falls back to `DEFAULT_CONFIG` in `server.py`. Highlights:

| Key | Meaning |
|-----|---------|
| `host`, `port` | bind address (default `0.0.0.0:443`) |
| `db_path` | SQLite file (sessions, users, SMS, call log) |
| `tls.cert` / `tls.key` | cert + key paths; auto-generated if missing |
| `tls.auto_generate` | shell out to openssl on first run (default true) |
| `tls.san` | subjectAltName for the cert (device IP / name) |
| `modem.device` | AT channel, default `/dev/smd11`; point elsewhere for tests |
| `modem.mock` | fake modem (same as `--mock`) |
| `stock_api.base_url` | stock MiFi API, default `http://127.0.0.1:80` (fallback only) |
| `platform.prefer_direct` | prefer direct system access over the stock proxy (default `true`) |
| `platform.wan_iface` / `lan_iface` | WAN (`rmnet0`) / LAN (`br0`) interface names |
| `platform.hostapd_conf` / `hostapd_bin` / `hostapd_entropy` | WiFi AP config + restart |
| `platform.dnsmasq_leases` | DHCP leases file for the WiFi-clients list |
| `platform.getprop_bin` | path to `getprop` for device info |
| `platform.netmode_at` | per-mode AT command map for net-mode **set** (empty → stock fallback) |
| `security.session_ttl` | session lifetime (s) |
| `security.login_rate_max` / `_window` | login rate limit |
| `security.admin_user` | admin username (default `admin`) |
| `sms_poll_seconds` | inbox poll interval; 0 disables the poller |
| `audio.enabled` / `host` / `port` | voice audio bridge |
| `system.services` | allow-listed services for `/api/system/service` |

CLI flags: `--config`, `--mock`, `--host`, `--port`, `--no-tls` (dev only).

---

## Security model

- **Auth** — admin username + password; password stored as a salted
  **PBKDF2-HMAC-SHA256** hash (200k iterations) in SQLite. First-run
  `POST /api/setup` sets it. Verified in constant time.
- **Sessions** — random opaque token in SQLite; the cookie carries
  `<token>.<hmac(server_secret, token)>`, so a tampered/forged cookie is
  rejected before any DB lookup. The `server_secret` is generated once and
  persisted in the DB. Cookie is `Secure; HttpOnly; SameSite=Strict`.
- **CSRF** — a per-session token returned by `GET /api/session`; required in the
  `X-CSRF-Token` header on every state-changing request (POST/DELETE/PUT),
  except `login`/`setup` which have no session yet.
- **Rate limiting** — sliding-window per-IP lockout on failed logins
  (default 5 failures / 5 min → `429`).
- **TLS** — HTTPS only (self-signed cert via openssl), TLS 1.2+ floor, HSTS
  header. Response hardening headers: `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`.
- **Stock proxy allow-list** — dangerous funcNos blocked; IMEI write gated.

All `/api/*` require auth **except** `login`, `logout`, `session`, `setup`.

---

## REST API contract

All request/response bodies are JSON. Auth is via the session cookie; writes
also need `X-CSRF-Token`. Errors return `{"error": "..."}` with an HTTP status
(400 bad request, 401 unauthenticated, 403 CSRF/forbidden, 404, 409 conflict,
429 rate-limited, 502 upstream/modem failure, 500 internal).

### Auth / session
| Method & path | Body | Response |
|---|---|---|
| `POST /api/login` | `{username,password}` | `{ok:true, csrf}` + sets cookie |
| `POST /api/logout` | — | `{ok:true}` clears cookie |
| `GET  /api/session` | — | `{authenticated, first_run, csrf}` |
| `POST /api/setup` | `{password}` (first-run only) | `{ok:true, username}` |

### Status
`GET /api/status` →
```json
{
  "signal": {"rsrp": -105.3, "rsrq": -11.0, "band": "B1", "earfcn": 525,
             "csq": 18, "bars": 4, "cells": [{"pci":310,"earfcn":6425,
             "rsrp":-78.1,"band":"B20","freq_mhz":800}]},
  "reg":  {"state": "registered", "operator": "Digi", "rat": "LTE"},
  "data": {"connected": true, "ip": "10.74.88.195",
           "up_bytes": 12345, "down_bytes": 67890},
  "uptime": 3600
}
```
`up_bytes`/`down_bytes` come **directly** from `/proc/net/dev` for `rmnet0`
(tx=upload, rx=download) when `prefer_direct`, falling back to stock funcNo 1003
(null if both unavailable). `data.ip` is from `AT+CGPADDR`, with a direct
`rmnet0` `SIOCGIFADDR` fallback. `signal.cells` lists every `$QCRSRP` cell; the
top-level `rsrp/band/earfcn` is the strongest (serving) cell.

### SIM / identity
| Method & path | Body | Response |
|---|---|---|
| `GET  /api/sim` | — | `{imei, imsi, iccid, sim_status, msisdn, model, manufacturer, firmware, android}` |
| `POST /api/sim/imei` | `{imei, confirm:true}` | `{ok, status, raw}` |

`model`/`manufacturer`/`firmware`/`android` come **directly** from `getprop`
(replacing stock funcNo 1029/3002); they are omitted if `getprop` is absent.

IMEI write is **dangerous**: requires `confirm:true`, 15-digit + Luhn
validation, and writes via `AT+WRIMEI=<imei>`.

### Network / APN
| Method & path | Body | Response |
|---|---|---|
| `GET  /api/network` | — | `{reg, contexts, data, netmode, dns, addresses}` |
| `POST /api/network/apn` | `{apn,user,pwd,auth[,name]}` | `{ok, at_status, stock}` |
| `POST /api/network/netmode` | `{mode}` | `{ok, source, at_status\|stock}` |

APN is set on the modem via `AT+CGDCONT` and, best-effort, via stock funcNo
1017. `netmode` GET is **direct** (modem RAT: `{mode, rat, source:"at"}`);
`dns` and `addresses` are **direct** (`resolv.conf` + `SIOCGIFADDR`). `netmode`
SET is direct via `platform.netmode_at` when configured, else falls back to
stock funcNo 1005 (`net_mode`: `0`=auto, `2`=4G-only, …).

### SMS
| Method & path | Body | Response |
|---|---|---|
| `GET    /api/sms?box=inbox\|sent` | — | `{box, messages:[...]}` |
| `POST   /api/sms` | `{to, text}` | `{ok, references, parts}` |
| `DELETE /api/sms/{id}` | — | `{ok, id}` |

Each message: `{id, box, address, text, ts, status, read, modem_index}`.
Inbox is populated by a background poller (`AT+CMGL` in PDU mode); sending uses
PDU mode and auto-selects GSM-7 or UCS2 (unicode), splitting long messages into
concatenated parts. `ts` is unix epoch.

### Calls
| Method & path | Body | Response |
|---|---|---|
| `GET  /api/call` | — | `{active:[...], log:[...]}` |
| `POST /api/call` | `{number}` | `{ok, status}` |
| `POST /api/call/hangup` | — | `{ok, status}` |

Dial uses `ATD<number>;`, hangup `ATH`, active calls from `AT+CLCC`. (No audio
hardware — see the audio bridge for 2-way voice.)

### WiFi (direct hostapd.conf; stock proxy fallback)
| Method & path | Body | Response |
|---|---|---|
| `GET  /api/wifi` | — | `{ssid, password, encryption, max_clients, channel}` |
| `POST /api/wifi` | `{ssid,password,encryption,max_clients,channel}` | `{ok, encryption, restart, warn?, source}` |
| `GET  /api/wifi/clients` | — | `{clients:[{hostname,ip,mac}], source}` |
| `GET  /api/wifi/macfilter` | — | `{mode, macaddr_acl, accept:[], deny:[], source}` |
| `POST /api/wifi/macfilter` | `{mode,accept?,deny?}` | `{ok, mode, restart, source}` |

With `platform.prefer_direct:true` (default) these read/write the device
directly (`/data/misc/wifi/hostapd.conf`, `dnsmasq.leases`, `/proc/net/arp`)
and restart `hostapd`; the stock funcNos (1006/1007/1010/1052–1055) are used
only if the direct path fails. `encryption` is `open|wpa2|wpa2wpa3|wpa3`
(wpa3/mixed degrade to WPA2-CCMP on this hostapd, surfaced in `warn`).
`macfilter.mode` is `disabled|accept` (whitelist, `macaddr_acl=1`) `|deny`
(blacklist). `source` reports whether the answer came from `direct` or `stock`.

### System
| Method & path | Body | Response |
|---|---|---|
| `GET  /api/system` | — | `{firewall, tailscale, adguard, services, audio, device}` |
| `POST /api/system/reboot` | — | `{ok}` |
| `POST /api/system/service` | `{name, action}` | `{ok, code, stdout, stderr}` |

`service.name` must be in `config.system.services`; `action` in that service's
map (`start`/`stop`/`status`). Reboot runs `config.system.reboot_cmd`.

### Stock passthrough
`POST /api/ajax` `{funcNo, ...}` — forwards to the stock `/ajax` **only** if the
funcNo is allow-listed. Pass `{"danger": true, ...}` to permit a danger-gated
funcNo (2002). Returns the stock response verbatim.

---

## Stock `/ajax` proxy allow-list

Defined in `stockproxy.py`:

- **Allowed (read / WiFi / LAN):** `1001, 1002, 1003, 1005, 1006, 1007, 1009,
  1010, 1011, 1015, 1016, 1017, 1018, 1029, 1052–1055, 3002`.
- **Blocked always:** `2000` (EDL reboot), `2001` (ADB enable), `3000` (flash).
- **Danger-gated:** `2002` (IMEI write) — forwarded **only** with an explicit
  danger flag.

> Note on 2002: the stock UI on this firmware (V2.3.12) does **not** expose a
> 2002 write-IMEI function — IMEI writes on this board go through `AT+WRIMEI`
> (`POST /api/sim/imei`). 2002 is kept in the gated set exactly as specified so
> that if a variant firmware exposes it, it still requires the danger flag.

---

## Direct implementation (`prefer_direct`) — deleting the stock app

The cockpit no longer **depends** on the stock MiFi app
(`com.mifiservice.hello`) and its unauthenticated `:80 /ajax` backdoor for any
feature. `uzplatform.py` reads/writes the **real system** directly, and each
endpoint prefers it, falling back to the stock proxy only if a direct call
fails. The behaviour is controlled by the config flag **`platform.prefer_direct`**
(default **`true`**; set `false` to force the old stock-proxy behaviour).

| Feature (old funcNo) | Direct source (`uzplatform.py` / `modem.py`) |
|---|---|
| Traffic counters (`1003`) | `/proc/net/dev` — `rmnet0` rx=download / tx=upload (and `br0`) |
| WAN IP / registration IP | `AT+CGPADDR` (modem) + `SIOCGIFADDR` ioctl on `rmnet0`/`br0` |
| DNS servers | `resolv.conf` / dnsmasq upstreams (+ `AT` on the modem) |
| WiFi status (`1006`) | parse `/data/misc/wifi/hostapd.conf` |
| WiFi config (`1007`) | rewrite `hostapd.conf` (ssid/passphrase/channel/wpa/pairwise/max_num_sta) + **restart hostapd** |
| WiFi clients (`1010`) | `/data/misc/dhcp/dnsmasq.leases` + `/proc/net/arp` (on `br0`) |
| MAC filter (`1052–1055`) | hostapd `macaddr_acl` + `accept_mac_file`/`deny_mac_file` + restart |
| Device info (`1029`/`3002`) | `getprop` (`ro.product.model`, `ro.build.*`, …) + `AT` (IMEI) |
| Net mode get (`1005`) | modem RAT via `AT+COPS?`/`AT+CEREG?` |
| Net mode set (`1005`) | `AT` template in `platform.netmode_at` (see below) |

**WiFi encryption mapping** (API → `hostapd.conf`): `open` → remove all `wpa*`
keys; `wpa2` → `wpa=2 / wpa_key_mgmt=WPA-PSK / rsn_pairwise=CCMP / wpa_pairwise=CCMP`;
`wpa2wpa3` / `wpa3` → same as `wpa2` with a `warn` (this KitKat-era hostapd has
no WPA3/SAE). WPA passphrases are validated to 8..63 chars. `hostapd.conf` is
rewritten **order-preserving** (comments and unrelated keys untouched) via an
atomic temp-file replace, then hostapd is restarted (its running argv is
captured and relaunched identically; `netd` may also respawn it).

**Net mode set** is the one action without a verified direct AT command on this
board: `GET` is always direct (derived from the modem RAT), but `SET` needs a
modem-specific command. Put the verified command(s) in
`platform.netmode_at` (e.g. `{"auto":"AT…","4g":"AT…"}`); until then `SET`
falls back to stock funcNo `1005`. This is the **only** residual stock
dependency, and it is read-only-safe (the app can still be deleted — net-mode
set simply becomes a no-op/`502` until the AT command is filled in).

### Taking over the plumbing — `deploy/uz-netservices.sh`

Before the stock app is deleted, run `deploy/uz-netservices.sh` (busybox `sh`,
as root, on the **host Android side**, not inside the chroot) to make the
device self-sufficient. It is **idempotent and safe to run while the stock app
is still up** — every step is guarded and logged, and nothing tears down
working state:

1. verify `br0`/`192.168.100.1` (built by `initmifiservice.sh` — **kept**);
2. ensure `ip_forward=1` *(normally netd)*;
3. ensure `MASQUERADE -o rmnet0` NAT *(normally netd; vendor
   `natctrl_nat_POSTROUTING` chain preferred)*;
4. (re)start **hostapd** with the conf *(normally netd; left alone if already
   running)* — **newly needed** only once the app/netd stops doing it;
5. (re)start **dnsmasq** for `br0` (range `192.168.100.100–200`, gateway/DNS
   `192.168.100.1`, leases `/data/misc/dhcp/dnsmasq.leases`) *(normally netd)*;
6. **data dial fallback** — if `rmnet0` has no IPv4, define+activate the PDP
   context over `/dev/smd11` (`AT+CGDCONT` / `AT+CGACT=1,1`). This is the only
   genuinely new piece; the RIL/app normally dials.

---

## Audio bridge (WebSocket PCM)

The board has **no codec/mic/speaker**, but the MSM8916 DSP exposes the voice
PCM in software via **tinyalsa**. `audiobridge.py` bridges it to a browser over
a WebSocket so you can "use the phone" from a device that *does* have a mic and
speaker.

- **Feature-flagged:** off by default (`audio.enabled`), and auto-disables if
  `tinycap`/`tinyplay` are not on PATH. `GET /api/system` reports availability
  under `audio`.
- **Separate port:** runs on its own listener (`audio.port`, default `8089`),
  sharing the same login (the WS upgrade must carry a valid session cookie).
  It can also run as a standalone process:
  `python3 audiobridge.py --config config.json`.

### WebSocket endpoint
```
wss://<device>:8089/ws/audio      (ws:// if TLS disabled)
Cookie: cockpit_session=<signed session cookie>   # required
```

### PCM / WS frame format
- **Binary WebSocket frames**, raw **PCM**, no container/header.
- **Downlink (server → browser):** the far end's voice. The browser plays these
  frames (Web Audio API).
- **Uplink (browser → server):** the browser's mic PCM. Fed to `tinyplay` as the
  near-end voice (the "virtual microphone").
- **Format (default, CONFIG in `audiobridge.py`):** `S16_LE`, **mono**,
  **8000 Hz** (narrowband CS voice). Use **16000 Hz** for wideband/VoLTE. Frame
  size default 640 bytes = 20 ms @ 8 kHz/16-bit.
- Byte order little-endian; samples are signed 16-bit.

Text frames are not used for audio. Close the socket to tear down the session
(tinycap/tinyplay are terminated and the tinymix routes reverted).

---

## File map

| File | Purpose |
|---|---|
| `server.py` | HTTPS REST server, routing, auth gate, CSRF, SMS poller, bootstrap |
| `modem.py` | thread-safe AT serializer over `/dev/smd11` + high-level modem ops + `MockModem` |
| `smspdu.py` | SMS PDU encode/decode (GSM-7 + UCS2, concatenation) |
| `db.py` | SQLite: users, sessions, SMS, call log, meta |
| `auth.py` | PBKDF2 passwords, signed-cookie sessions, CSRF, login rate limit |
| `uzplatform.py` | **direct** system access (hostapd.conf, dnsmasq.leases, `/proc`, ioctl, getprop) replacing the stock `/ajax` backdoor + `MockPlatform` |
| `stockproxy.py` | allow-listed passthrough to stock `/ajax` (**fallback only** when `prefer_direct`) |
| `deploy/uz-netservices.sh` | idempotent busybox-sh takeover of the MiFi plumbing (NAT/hostapd/dnsmasq/data-dial) so the stock app can be deleted |
| `tlsutil.py` | first-run self-signed cert via openssl CLI + SSLContext |
| `wsframe.py` | minimal RFC6455 WebSocket server (stdlib only) |
| `audiobridge.py` | tinyalsa ↔ WebSocket PCM voice bridge + CONFIG constants |
| `logutil.py` | structured `key=value` logging |
| `config.example.json` | annotated config template |
| `run.sh` | launcher (env-driven; used by boot-hook.sh) |
| `requirements.txt` | dependency notes (zero Python deps; apk packages) |

---

## Things to fill in on the device

These are the only device-specific unknowns; all are clearly marked in code.

1. **TLS cert identity** — `config.tls.san` / `cn` should match how you reach
   the device (IP `192.168.100.1` and/or a name). Self-signed, so browsers warn
   once; import the cert to trust it.

2. **Audio bridge — tinyalsa indices & tinymix routes** (in `audiobridge.py`,
   top CONFIG block — **all placeholders until the hardware spike**):
   - `CARD` — ALSA card index for `msm8x16sndcardm` (expected `0`).
   - `CAP_DEVICE` — capture (downlink) PCM device. Candidate: **AFE-PROXY TX =
     card0 device8**. Alternative: **Voice Downlink Capture = device35**.
   - `PLAY_DEVICE` — playback (uplink/"virtual mic") PCM device. Candidate:
     **AFE-PROXY RX = card0 device7**. Alternative: **Voice Farend Playback =
     device36**.
   - `CAP_RATE` / `PLAY_RATE` — `8000` (narrowband CS) or `16000` (WB/VoLTE).
   - `TINYMIX_ROUTES_ON` / `_OFF` — the exact `tinymix set ...` commands that
     connect the live voice session to the proxy/farend ports. **Unknown until
     the spike** (the device has ~595 tinymix controls incl. `PROXY_RX` routing
     and `Voice Rx/Tx` gain/mute). Verify device indices with `tinypcminfo` and
     `cat /proc/asound/pcm`.
   - `TINYCAP`/`TINYPLAY` tool names if not on PATH.
   - Note: some Android `tinycap` builds only write to a file, not stdout. If
     `tinycap -` doesn't stream, use a named pipe (`mkfifo`) and point tinycap
     at it, reading the pipe in `_pump_downlink`.

3. **System service commands** — `config.system.services` and `reboot_cmd`
   assume Alpine OpenRC (`rc-service ...`). Adjust to how Tailscale/AdGuard are
   actually started in your chroot (they may be launched directly from
   `boot-hook.sh` with no init). `firewall_cmd` assumes `iptables` is present.

4. **openssl** — `apk add openssl` before first run, or pre-place cert/key and
   set `tls.auto_generate:false`.

5. **One-time tests to confirm live behaviour** (optional, cost a message/call):
   send one SMS, and dial a known number for the audio spike.
