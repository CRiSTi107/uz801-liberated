# UZ801 4G Dongle - Modern Web UI

A self-contained replacement management UI for the **UZ801** 4G USB dongle
(Qualcomm MSM8916, firmware `UZ801-V2.3.12`). Vanilla HTML/CSS/JS, **no build
step, no CDN, no external network dependencies**. It talks to the device's
`POST /ajax` API through a tiny local reverse proxy.

```
webui/
  proxy.py        # Python 3 stdlib: serves the UI + forwards /ajax to the device
  index.html      # single-page app shell
  css/styles.css  # dark theme
  js/api.js        # api(funcNo, params) client + signal/byte helpers
  js/app.js        # UI wiring, polling, actions
  README.md        # this file
```

## Run it

```bash
cd webui
python3 proxy.py
# then open  http://localhost:8088/
```

No `pip install` needed - only the Python 3 standard library is used.

### Change the device IP

The device base URL defaults to `http://192.168.100.1`. Override it (and the
port / bind address) with environment variables:

```bash
DONGLE_URL=http://192.168.1.1 python3 proxy.py     # different device IP
PORT=9000 python3 proxy.py                          # listen on :9000
HOST=0.0.0.0 python3 proxy.py                        # expose on the LAN
```

You can also edit the `DEVICE_BASE`, `PORT`, `HOST` constants at the top of
`proxy.py`.

## Why a local proxy? (CORS / mixed-content)

The dongle API is at `http://192.168.100.1/ajax` over **plain HTTP**, and the
device sends **no CORS headers**. A browser page served from any other origin
cannot `POST` to it:

- **CORS** blocks cross-origin requests the device doesn't opt into.
- **Mixed-content** blocks an `https` page from calling a plain-`http` endpoint.

`proxy.py` solves both by serving the UI *and* forwarding `/ajax` from the same
origin (`http://localhost:8088`). It passes JSON and multipart firmware uploads
straight through, and forwards the session cookie both ways so login (funcNo
1000) persists. When the device is unreachable it returns HTTP 502 with a JSON
error so the UI can show its offline banner.

> **Local only.** This UI must run on your own machine on the same LAN as the
> dongle. Do not host it publicly - it is a privileged admin tool for a device
> on your network.

## API contract (reverse-engineered)

Single endpoint `POST /ajax`, body is a JSON string `{"funcNo":N, ...}`.
Response: `{"results":[{...}], "error_info":"none", "flag":"1"}`.
`flag:"1"` = success; otherwise `error_info` holds the message. Some status
reads work without login.

| funcNo | Purpose | Notes |
|-------:|---------|-------|
| 1000 | Login (`username`,`password`) | default admin/admin |
| 1001 | Network status | rssi, oper, netstatus, netmode |
| 1002 | IP / DNS info | IP, mask, dns1/2, wlan_ip, ssid |
| 1003 | Traffic | up_bytes, down_bytes, client_num, maxSta |
| 1004 | Set connection mode | **field name unverified** |
| 1005 | Set net mode | 0=Auto, 1=3G-only, 2=4G-only |
| 1006-1010 | WiFi settings / security | ssid, mac, channel, pwd, encryp_type |
| 1011, 1052-1055 | MAC address filter | **unverified** |
| 1013 | Reboot | confirm required |
| 1014 | Factory reset | confirm required |
| 1015 | SIM status | sim_status, imsi, iccid |
| 1016-1018 | WAN / APN profile + connect | **unverified** |
| 2001 | Enable USB debug / root ADB | Developer area, confirm required |
| 3000 | Apply firmware upgrade | confirm required |
| 3001 | Delete staged package | |
| 3002 | Device / firmware info | device_name, sw_version |

Firmware **upload** is a multipart `POST /ajax` with form field `upload_file`.

## Unverified funcNos - may need field tweaks

The following were not confirmed against the device's own JS. The forms send
sensible field names; **if the device rejects a request, adjust the fields** in
`js/app.js` (search for the funcNo):

- **APN 1016-1018** - sent as `{apn, user, pwd, auth}`. The real field names
  are unknown. 1018 (connect) is sent with no params.
- **MAC filter 1011 / 1052-1055** - read via 1052, applied via 1055 as
  `{mode, mac_list}`. Mode codes (0 off / 1 whitelist / 2 blacklist) and the
  list field name are guesses.
- **Connection mode 1004** - sent as both `{conn_mode:"auto|manual"}` and
  `{mode:1|0}` to improve the odds one is accepted.
- **WiFi save 1009** - field names (`ssid`,`pwd`,`encryp_type`,`channel`) are
  the likely set; verify against your firmware.

A **Raw funcNo console** on the Developer tab lets you send any funcNo with
arbitrary JSON params to help verify/adjust these.

## Features

- **Dashboard** with live cards (connection, operator, signal bars + dBm, net
  mode, IP/DNS, up/down data in human-readable bytes, client count) that polls
  1001/1002/1003/1015 every few seconds, with a **pause toggle** and a
  configurable interval (Settings).
- **Login gate** (1000) with a read-only "skip" option, since status reads work
  without auth. Session cookie is forwarded by the proxy.
- **Network / SIM / APN / WiFi / MAC Filter / Firmware / Developer / Settings**
  tabs.
- **Signal helper** maps CSQ rssi (0-31, 99=unknown) to dBm (`-113 + 2*rssi`)
  and 0-5 bars.
- **Destructive actions** (reboot 1013, factory reset 1014, enable ADB 2001,
  apply firmware 3000) require an explicit confirm dialog.
- **Graceful offline state** when the proxy/device is unreachable.
- Dark, responsive, accessible; works on a phone. No credentials are hardcoded
  (login defaults to blank with an admin/admin hint).

## A note on SIM status

On this hardware the web UI's SIM panel can be **unreliable** (it may report
"SIM Absent" while the modem actually reports `+CPIN: READY`). If the dashboard
disagrees with `modem_at` output over ADB, trust the AT output. The real common
blocker is *not registered + empty APN* rather than a missing SIM.
