# Security Audit — UZ801 4G USB Dongle Firmware (V2.3.12)

Static firmware audit. **No code from the images was executed** — analysis was
done entirely by read-only extraction, `strings`/`grep`, and decompilation.

- Device: UZ801 4G MiFi/USB dongle, Qualcomm MSM8916, Android 4.4.4 (KitKat), fw `UZ801-V2.3.12`, build `eng..20240812`.
- Admin UI: `http://192.168.100.1/` (Jetty "4G Modem"); single API endpoint `POST /ajax` (JSON `{funcNo:N,...}`).
- Images audited: `system.img` (ext4 rootfs), `boot.img` + `recovery.img` (kernel+ramdisk), `modem.img`, `persist.img`, bootloader ELFs.

## How to reproduce the extraction

```
# scratch dir (never touch dump/)
SCR=/tmp/.../scratchpad/audit
# ext4 rootfs
debugfs -R 'rdump / '"$SCR"'/extract/system' dump/system.img
# boot/recovery ramdisks (Android bootimg, page 2048): python carver -> gzip -> cpio
python3 -I carve_boot.py dump/boot.img     $SCR/extract/boot
python3 -I carve_boot.py dump/recovery.img $SCR/extract/recovery
(cd $SCR/extract/boot/rd && cpio -idmu --no-absolute-filenames < ../ramdisk.cpio)
# admin app
unzip -oq system/priv-app/MifiService.apk -d $SCR/mifi_apk        # web assets (assets/jetty, assets/jetty2)
jadx -d $SCR/mifi_jadx --no-res system/priv-app/MifiService.apk   # Java handlers from classes.dex
```

Key source: `com.mifiservice.server.AjaxSevlet` (the `/ajax` dispatcher),
`com.mifiservice.server.JServer` (embedded Jetty), `com.mifiservice.ota.Updater`
(OTA), `com.mifiservice.config.MifiConfiguration` (credentials).

---

## Severity summary

| # | Severity | Finding |
|---|----------|---------|
| 1 | CRITICAL | `/ajax` API has **no authentication** — every function reachable without login, server runs as `uid=system` |
| 2 | CRITICAL | funcNo **2001** enables ADB+DIAG and reboots — unauthenticated → root shell |
| 3 | CRITICAL | ADB **root by default** (`ro.secure=0`, `ro.debuggable=1`, usb config includes `adb`) + `persist.adb.tcp.port=7628` (root ADB over TCP on LAN) |
| 4 | CRITICAL | Unauthenticated firmware upload + funcNo **3000** `doUpdateNoMd5()` → flashes unsigned/unverified `update.zip`; recovery verifies only against **AOSP test-key** |
| 5 | HIGH | Cleartext-HTTP OTA phone-home to **`120.77.62.229:8080`** every 30 min, leaks IMEI, MITM → remote code execution |
| 6 | HIGH | funcNo **2002** writes arbitrary **IMEI** (`AT+WRIMEI`), unauthenticated |
| 7 | HIGH | funcNo **2000** reboots to bootloader (EDL/fastboot), unauthenticated → offline flashing |
| 8 | HIGH | Unauthenticated info disclosure: WiFi PSK in plaintext, IMEI/IMSI/ICCID, client list |
| 9 | HIGH | Firmware signed with **AOSP test-keys** (`ro.build.tags=test-keys`) — anyone can sign system/OTA |
| 10 | MEDIUM | Setuid-root `su` and `tcpdump` in `/system/xbin` (mode 06755) |
| 11 | MEDIUM | Default credentials: web `admin`/`admin`, WiFi `1234567890` |
| 12 | MEDIUM | Qualcomm **DIAG** port exposed by default USB config (`diag`) + `/dev/smd11` world-writable (0666) |
| 13 | MEDIUM | funcNo 1013/1014/1024 unauth reboot / factory reset (DoS); funcNo 1010/1025 unauth WiFi password change |
| 14 | MEDIUM | Root serial console (`console /system/bin/sh`, `user_debug=31`) |
| 15 | LOW | EOL/unpatched stack (Android 4.4.4 KitKat 2014, SDK19, Dalvik, old embedded Jetty, busybox toolkit incl. telnetd/ftpd/httpd) |
| 16 | LOW | Request bodies (incl. passwords) written to logcat in cleartext |

---

## 1. CRITICAL — `/ajax` API has no authentication (complete auth bypass)

The web backend is an embedded Jetty server inside `MifiService.apk`, started by
`com.mifiservice.service.MifiService`:

- `service/MifiService.java:38` → `JServer mServer = new JServer(80);`
- `server/JServer.java` → `new Server(this.mPort)` with **no bind address** → listens on **0.0.0.0:80** (all interfaces: WiFi clients and USB-RNDIS host). Only two handlers are registered: `AjaxSevlet` at `/ajax` and a static `ResourceHandler`. **There is no Jetty security handler, no auth filter, no session/cookie check.**

`server/AjaxSevlet.java:doPost()` reads the JSON body, extracts `funcNo`, and
executes the corresponding action directly. The only function that checks a
password is the "login" itself (funcNo 1000, line 158-178), which merely returns
`flag:1/0`. **No other funcNo consults any session or login state** — the login
is purely a client-side/cosmetic gate. Any host that can reach the device can
invoke any function with a bare HTTP POST, e.g.:

```
curl -s http://192.168.100.1/ajax -d '{"funcNo":2001}'          # enable ADB+diag, reboot
curl -s http://192.168.100.1/ajax -d '{"funcNo":1002}'          # dump WiFi PSK, IMEI, DNS
curl -s http://192.168.100.1/ajax -d '{"funcNo":2002,"imei":"..."}'  # rewrite IMEI
curl -s http://192.168.100.1/ajax -d '{"funcNo":1014}'          # factory reset
```

Worse, `AndroidManifest.xml` declares `android:sharedUserId="android.uid.system"`,
so this unauthenticated server runs with **system privileges**, holding
`WRITE_SECURE_SETTINGS`, `MASTER_CLEAR`, `MOUNT_UNMOUNT_FILESYSTEMS`,
`MODIFY_PHONE_STATE`, `DEVICE_POWER`, etc.

### Full funcNo map (from `AjaxSevlet.java`, with auth = none for all)

| funcNo | Line | Action | Notes |
|--------|------|--------|-------|
| 1000 | 158 | Login (admin + `loginPassword`) | only cosmetic; other calls don't require it |
| 1001 | 180 | Net status (rssi/netmode/oper) | |
| 1002 | 200 | IP/DNS/SSID + **WiFi PSK plaintext** | info leak |
| 1003 | 218 | Traffic + GPS lat/long + clients | |
| 1004 | 235 | Set mobile-data auto-connect | |
| 1005 | 252 | Set preferred network type | |
| 1006 | 268 | WiFi AP status | |
| 1007 | 288 | Set SSID / maxSta | |
| 1008 | 310 | Restart AP | |
| 1009 | 318 | Get encrypt type + **WiFi PSK plaintext** | info leak |
| 1010 | 331 | Set WiFi encryption + password | unauth WiFi takeover |
| 1011 | 347 | DHCP + connected-client list (name/ip/mac) | info leak |
| 1012 | 376 | Set WLAN DNS1/DNS2 | **DNS hijack** vector |
| 1013 | 387 | Reboot device | DoS |
| 1014 | 395 | Factory restore | DoS/destructive |
| 1015 | 403 | SIM status + **IMSI + ICCID** | info leak |
| 1016 | 417 | List APNs (incl. user/password) | |
| 1017 | 455 | Add APN | |
| 1018 | 469 | Set current APN | |
| 1020 | 480 | Change login password (checks old) | |
| 1021 | 496 | Read `adb_enabled` | |
| 1022 | 509 | **Set `adb_enabled`** via `WRITE_SECURE_SETTINGS` | secondary ADB-enable path |
| 1023 | 518 | Traffic counters | |
| 1024 | 531 | Reboot device | DoS |
| 1025 | 539 | Set SSID+password (WPA2) | unauth WiFi takeover |
| 1026/1027 | 555/567 | Get/set DHCP range | |
| 1028 | 580 | Client list (ip/mac) | |
| 1029 | 602 | **IMEI** + manufacturer + fw + dBm | info leak |
| 1052-1055 | 622+ | MAC filter get/set/commit | |
| **2000** | 681 | **Reboot to bootloader** (EDL/fastboot) | see Finding 7 |
| **2001** | 689 | **Enable ADB+DIAG USB config + reboot** | see Finding 2 |
| **2002** | 703 | **Write arbitrary IMEI** (`AT+WRIMEI`) | see Finding 6 |
| **3000** | 718 | **Apply update** `doUpdateNoMd5()` | see Finding 4 |
| 3001 | 726 | Delete `/data/update.zip` | |
| 3002 | 743 | Device name + sw version | |

> Correction to prior project notes: funcNo **2002 (write IMEI) and 1029 (device
> info) DO exist as live server-side handlers** in `AjaxSevlet.java`, even though
> the shipped UI JavaScript does not call them. They are reachable by a direct POST.

---

## 2. CRITICAL — funcNo 2001: unauthenticated ADB/DIAG enable → root shell

`AjaxSevlet.java:689-702`:

```java
if (funcNo == 2001) {
    ...
    Runtime.getRuntime().exec("setprop persist.sys.usb.config rndis,serial_smd,diag,adb");
    Runtime.getRuntime().exec("sync");
    this.mDeviceController.rebootDevice();
    ...
}
```

Triggered by the hidden page `assets/jetty2/usbdebug.html` + `js/usbdebug.js`
(which sends `{funcNo:2001}` with no credentials). Because ADB on this build is
root (Finding 3), this single unauthenticated request turns the dongle into a
rootable device and also re-exposes the Qualcomm DIAG interface.

---

## 3. CRITICAL — ADB is root by default; ADB-over-TCP on the LAN

`boot/rd/default.prop` (and identical in `recovery/rd/default.prop`):

```
ro.secure=0
ro.debuggable=1
persist.sys.usb.config=adb
```

`system/build.prop`:

```
persist.sys.usb.config=rndis,serial_smd,diag,adb
persist.adb.tcp.port=7628
```

- `ro.secure=0` → **adbd does not drop privileges: `adb shell` is root (uid 0).**
- `boot/rd/init.rc:450` → `on property:ro.debuggable=1` … `:462 start adbd` — adbd is started automatically.
- The default USB composition already includes `adb` → **ADB is enabled out of the box over USB**, no user action needed.
- `persist.adb.tcp.port=7628` is read by AOSP adbd at startup; with it set, **adbd also listens on TCP port 7628**. Since the device bridges WiFi + RNDIS to `192.168.100.1`, this exposes a **root ADB shell over the network** (`adb connect 192.168.100.1:7628`) to any WiFi client — no authentication, no RSA-key prompt on 4.4.
  - Confidence: the property is present and this is standard AOSP adbd behavior; exact live state should be confirmed on-device, but the configuration intent is unambiguous.

This is effectively a built-in root backdoor reachable from the local network.

---

## 4. CRITICAL — Unauthenticated, unsigned firmware upload & flash

**Upload** (`AjaxSevlet.java:85-128`): any multipart POST to `/ajax` is written to
the hardcoded path `/data/update.zip` (no path traversal, but also **no auth, no
signature, no version/model check**). Client: `assets/jetty2/js/upgrade.js`
→ `uploadFile()` POSTs `upload_file` to `/ajax`.

**Flash** (`AjaxSevlet.java:718`, funcNo 3000):

```java
this.mUpdater.doUpdateNoMd5();
```

`ota/Updater.java`:

```java
public void doUpdateNoMd5() {
    File file = new File("/data/update.zip");
    if (file.exists()) { recoveryMode(this.powerManager); }   // no md5, no signature
}
private void recoveryMode(...) {
    ... command.write("--update_package=/data/update.zip"); pm.reboot("recovery");
}
```

The app applies the attacker-supplied zip with **no integrity check at all**. The
only remaining barrier is recovery's own signature check — which is defeated by
Finding 9 (test-keys). Net result: **any LAN/WiFi client can install a persistent
malicious firmware image** (two unauthenticated POSTs).

---

## 5. HIGH — Cleartext-HTTP OTA phone-home to a hardcoded Chinese cloud host

`ota/Updater.java`:

```java
// every 30 min (timer: 120s initial, 1,800,000 ms period)
String url = "http://120.77.62.229:8080/file/checkUpdate?&device=" + deviceName
             + "&swv=" + swVersion + "&imei=" + IMEI;        // leaks IMEI over cleartext HTTP
...
String url = "http://120.77.62.229:8080/file/doDownload?md5=" + md5 + "&imei=" + IMEI;
down.setDestinationInExternalPublicDir("/data", "update.zip");
...
doUpdate();  // compares file md5 to the md5 the SAME server returned, then recoveryMode()
```

- `120.77.62.229` is an **Alibaba Cloud (Aliyun)** address; the OTA channel is
  **plaintext HTTP**, so a network MITM (carrier, rogue AP, upstream) fully
  controls both the advertised `md5` and the downloaded file. `doUpdate()`'s
  md5 "check" is not a signature and provides no protection against a MITM.
- Combined with test-keys recovery (Finding 9), MITM of this endpoint yields
  **remote persistent code execution** on every device on this firmware.
- Also a privacy issue: the device beacons its **IMEI** to a fixed server every
  30 minutes. This is the only external endpoint found (full URL/IP grep of the
  dex and web assets returned only `120.77.62.229` plus LAN IPs and `8.8.8.8`).

---

## 6. HIGH — funcNo 2002: unauthenticated arbitrary IMEI write

`AjaxSevlet.java:703` → `WanDataController.setImei()`:

```java
public boolean setImei(String imei) {
    String string = "AT+WRIMEI=" + imei;
    sendAtBySmd(string);              // modem_at AT+WRIMEI=<imei> over /dev/smd11
    return checkImeiByAt(imei);
}
```

Any unauthenticated caller can rewrite the device IMEI (identity spoofing / evasion).

---

## 7. HIGH — funcNo 2000: unauthenticated reboot to bootloader

`AjaxSevlet.java:681` → `DeviceController.rebootBootloader()` → `reboot("bootloader")`
(page `reboot-bootloader.html`). Drops the device into fastboot/EDL, enabling
offline flashing / partition dumping by anyone who can send one POST and has USB
access after reboot.

---

## 8. HIGH — Unauthenticated information disclosure

Without login an attacker can read: WiFi PSK in plaintext (funcNo 1002, 1009),
IMEI (1002 login response, 1029), IMSI + ICCID (1015), APN usernames/passwords
(1016), GPS coordinates (1003), and the full connected-client list with hostnames
and MACs (1011, 1028).

---

## 9. HIGH — Firmware signed with AOSP test-keys

`system/build.prop`: `ro.build.tags=test-keys`,
`ro.build.fingerprint=qcom/msm8916_32_512/...:userdebug/test-keys`.

`recovery/rd/res/keys` is the well-known public **AOSP test key**
(RSA exponent `0xc926ad21`, the canonical AOSP `testkey`). Because the signing
key is public, **anyone can sign an OTA/system image that recovery will accept**,
nullifying the signature check that would otherwise limit Finding 4/5.

---

## 10. MEDIUM — Setuid-root binaries in /system/xbin

From `debugfs stat` on `system.img` (true on-disk modes):

```
/xbin/su       Mode: 06755  User:0 Group:0   (setuid root)
/xbin/tcpdump  Mode: 06755  User:0 Group:0   (setuid root)
```

`su` is a setuid-root shipped in the system image → any process/UID on the device
can escalate to root. `tcpdump` setuid-root allows any user to sniff traffic.
`/system/bin/busybox` is present and its applet table includes `telnetd`, `ftpd`,
`httpd`, `tftp`, `wget`, `nslookup` — a ready post-exploitation / remote-access
toolkit (not started by init, but trivially launchable once code-exec/root is had).

---

## 11. MEDIUM — Default / hardcoded credentials

`config/MifiConfiguration.java`:

```java
public String loginPassword = "admin";       // web admin default
public String WIFIpassword  = "1234567890";   // getWifiDefaultKey() also returns "1234567890"
public String WIFIssid      = "4G MIFI-DA7";   // runtime SSID 4G-UFI-<mac3>
```

Web login is `admin`/`admin`; default WPA key is `1234567890`. Config stored
world-readably at `/data/mificonfig.json` in cleartext (passwords in plaintext).

---

## 12. MEDIUM — Qualcomm DIAG exposed; modem AT channel world-writable

Default USB composition (`build.prop`) is `rndis,serial_smd,diag,adb` — the
Qualcomm **DIAG** port is exposed by default, allowing NV/EFS read-write, IMEI
changes, and memory access via QPST/QXDM-class tools over USB.

`bin/initmifiservice.sh` (run as root at boot, `init.target.rc:101`):

```
chmod 666 /dev/smd11      # modem AT channel world-writable: any local app/uid can issue AT commands
iptables -t mangle -I PREROUTING -i rmnet0 -j TTL --ttl-set 64   # TTL mangling (carrier tether-hiding)
iptables -t mangle -A POSTROUTING -j TTL --ttl-set 64
```

(`/dev/smd11` 0666 = local modem-command access; TTL rewrite is tethering
evasion, noted for completeness, not a remote vuln.)

---

## 13. MEDIUM — Unauthenticated destructive / config-change functions

Reboot (1013/1024), factory reset (1014), WiFi credential change (1010/1025),
and WLAN DNS change (1012 — a **DNS-hijack** primitive against all clients) are
all reachable without authentication.

---

## 14. MEDIUM — Root serial console / debug kernel cmdline

`boot.img` cmdline: `... user_debug=31 androidboot.console=ttyHSL0`.
`init.rc:443` defines `service console /system/bin/sh` on the UART. With
`ro.debuggable=1` this yields a root shell on the serial console to anyone with
physical UART access.

---

## 15. LOW — End-of-life, unpatched software stack

Android 4.4.4 (KitKat, 2014; API 19), Dalvik, 3.x-era MSM8916 kernel, an old
embedded Eclipse Jetty, and the AOSP crypto stack of 2014 are all long past EOL
and carry numerous public CVEs (Stagefright, kernel privesc, etc.). `media.
stagefright.enable-http=true` is set. These are not individually verified here
but the base platform is unsupported and unpatchable in practice.

---

## 16. LOW — Sensitive data logged in cleartext

`AjaxSevlet` logs every request body to logcat: `Log.d("Ajax", "request: " + acceptjson)`
(lines 71, 154) — includes login and password-change payloads. Readable by any
app with `READ_LOGS` or via the root ADB shell.

---

## Notable non-findings / clarifications

- **No shell command injection via the web API.** The several
  `Runtime.getRuntime().exec(String)` calls that incorporate web input (APN name,
  SSID, IMEI, etc.) use the single-string `exec`, which tokenizes on whitespace
  and does **not** invoke a shell. So shell metacharacters (`;`, `|`, backticks)
  are not interpreted — this is argument injection at most, low impact. The
  `ShellUtils` su-exec helper exists but is not reached from the `/ajax` path.
- `/etc/passwd` and `/etc/shadow` do not exist (normal for Android's AID model);
  no extra blank-password Unix accounts.
- `system/etc/hosts` contains only `127.0.0.1 localhost` (no DNS tampering at rest).
- `system/etc/cdrom_install.iso` is a benign RNDIS-driver CD image
  (`README.TXT`, `ZERO.BIN` padding) — no autorun executable.
- Only one external network endpoint was found across the APK and all web assets:
  `120.77.62.229:8080` (Finding 5).

## Bottom line

This firmware behaves as a **remotely rootable device**: an unauthenticated web
API running at system privilege lets any WiFi/USB-adjacent attacker enable a root
ADB shell, rewrite the IMEI, change WiFi/DNS, factory-reset, and flash arbitrary
unsigned firmware; ADB-root is on by default; and the device auto-updates over
cleartext HTTP from a fixed Chinese cloud host (MITM → RCE) while beaconing its
IMEI. These are real, local-network-reachable backdoors, not theoretical.

---

## Deep audit (second pass) — 2026-11-07

Second, deeper static pass. Re-extracted every image read-only into a scratch
dir (`debugfs rdump` for the ext4 `system`/`persist`; a Python boot-image carver
+ `gzip`/`cpio` for the `boot`/`recovery` ramdisks; `7z` for APKs) and decompiled
`MifiService.apk`'s `classes.dex` with **androguard 4.1.4** (installed in a scratch
venv; `jadx`/`apktool` were absent). **No firmware code was executed.** Dump note:
the dump dir actually ships `.img` files (not `.bin`), and **does not contain
`userdata`/`cache` images** — so `/data` (runtime `mificonfig.json`, `update.zip`,
`hostapd.conf`, adb keys) could not be examined; those findings remain inferred
from code + init scripts. Setuid bits are **not** preserved by `debugfs rdump`, so
true on-disk modes were read directly with `debugfs -R 'ls -l'`.

The findings below are **new or materially sharpen** a prior finding; items that
merely re-confirm the first pass are listed under "Confirmations" at the end.

### D1. CRITICAL (sharpens #5) — the OTA host is a *direct* remote firmware-flash channel, no MITM required

Full decompile of `com.mifiservice.ota.Updater` (`Updater$CheckUpdateTask`,
`downloadApp`, `doUpdate`, `recoveryMode`) shows the "update check" is a complete
server-driven flash pipeline, and the md5 "check" is **not** an integrity control:

```java
// Updater$CheckUpdateTask.doInBackground(): the ENTIRE HTTP response body is taken as a string
String resp = NetUtil.httpGet("http://120.77.62.229:8080/file/checkUpdate?&device="+dev+"&swv="+swv+"&imei="+imei);
// onPostExecute(resp): if battery ok -> downloadApp(resp)
private void downloadApp(String p6){           // p6 = whatever the server returned
    new DownloadManager.Request(Uri.parse("http://120.77.62.229:8080/file/doDownload?md5="+p6+"&imei="+imei));
    ... setDestinationInExternalPublicDir("/data","update.zip"); this.downloadMd5 = p6; ...
}
// Updater$2.onReceive(DOWNLOAD_COMPLETE): copyFile("/sdcard/data/update.zip","/data/update.zip"); doUpdate();
public void doUpdate(){
    File f=new File("/data/update.zip");
    if(f.exists() && this.downloadMd5.equals(TextUtil.calcMd5(f)))   // compares file to the SAME server's string
        recoveryMode(powerManager);
}
private void recoveryMode(...){ // writes /cache/recovery/command then reboots to recovery, which flashes the zip
    new FileWriter(new File("/cache/recovery","command")).write("--update_package=/data/update.zip\n"); pm.reboot("recovery");
}
```

- The `md5` that `doUpdate()` validates against is **the very string the server
  returned** from `checkUpdate` — so the server (or anyone controlling the
  endpoint) chooses both the file bytes *and* the "expected" hash. There is **no
  signature, no public-key pin, no version/model/anti-rollback check** at the app
  layer. The only residual gate is recovery's package signature — defeated by the
  public test-key (see D2 / prior #9).
- Evidence (decompiled): `scratchpad/out_updater.txt`; source paths
  `com/mifiservice/ota/Updater`. Timer fires `doCheckUpdate()` 120 s after boot,
  then **every 1 800 000 ms (30 min)** (`Updater(Context)` ctor).
- **Impact / answer to "can the remote host push code or commands?": YES — it can
  push *code*** (an arbitrary OTA/recovery package that is flashed and persists
  at system level, surviving reboot/factory-reset-of-data). It does **not** expose
  a live remote *shell/command* string via this channel (the response is only
  consumed as the md5/download selector), but an attacker-chosen recovery package
  is strictly more powerful than a shell. Two independent ways to control it:
  1. **Own/compromise `120.77.62.229`.** It is a **hard-coded bare IPv4** (Aliyun),
     not a DNS name — if that Alibaba Cloud address is ever reassigned/lapses, the
     new tenant inherits push-flash control of every device still polling it.
  2. **Cleartext-HTTP MITM** (carrier/rogue-AP/upstream) — same result, per #5.
- Extra exposure: the package is first written to **`/sdcard/data/update.zip`
  (external, world-readable)** and then copied to `/data/update.zip`, widening who
  can read/stage it locally.

### D2. HIGH (sharpens #9) — system-side OTA trust store is literally the AOSP test key

`/system/etc/security/otacerts.zip` contains a single entry named
**`testkey.x509.pem`** (the public AOSP `build/target/product/security/testkey`).
This is the trust store used by the Android-framework `RecoverySystem.verifyPackage`
path, and it is the *same* public test key as recovery's `res/keys`
(exponent `0xc926ad21`, confirmed in `recovery/rd/res/keys`). So **both** the
framework-side and recovery-side OTA verifiers trust a key whose private half is
public — anyone can sign a package that passes. Evidence: `7z l otacerts.zip` →
`testkey.x509.pem`; `build.prop` `ro.build.tags=test-keys`,
`ro.build.type=userdebug`.

### D3. MEDIUM (sharpens #10) — three MORE setuid-root binaries beyond su/tcpdump

True on-disk modes (`debugfs -R 'ls -l /xbin' system.img`) show setuid-root
(`106755`, uid 0/gid 0) not only on `su` and `tcpdump` but also:

```
106755  0 0  /system/xbin/procmem      # read arbitrary process memory
106755  0 0  /system/xbin/procrank
106755  0 0  /system/xbin/librank
```

`procmem` setuid-root lets **any local uid dump the memory of any process**
(WiFi PSK, session state, keys, modem/IMSI data held by system/radio processes) —
a root-capable info-leak / escalation primitive, on top of the `su` in #10.
`/system/bin/netcfg` is setgid (`102750`). (All other `/bin`,`/xbin` entries are
plain `100755`.)

### D4. MEDIUM — busybox 1.31.0 ships telnetd / httpd / ftpd / tftpd / **inetd** / **crond** / nc

`/system/bin/busybox` is a **modern busybox 1.31.0** (unusually new for a 2014
KitKat base) whose compiled-in applet table includes `telnetd`, `httpd`, `ftpd`,
`ftpget/ftpput`, `tftpd`/`tftp`, **`inetd`**, **`crond`**, `nc`, `wget`,
`nslookup` (strings from `/system/bin/busybox`). The first pass noted a subset; the
new items are **`inetd`** and **`crond`** — a super-server and a cron daemon that
would auto-launch listeners / scheduled jobs *if* an `/etc/inetd.conf` or a
crontab were dropped. None is present at rest (no `inetd.conf`/`crontab` found on
`system` or `persist`), and no init service starts busybox, so this is a
ready-to-use remote-access + persistence toolkit for the post-exploitation stage
rather than an active listener. Severity MEDIUM because `inetd`/`crond` +
`telnetd` are one dropped config file away from an unauthenticated telnet root
shell on a box that is already root-by-default.

### D5. LOW — SELinux is not meaningfully confining; system-uid can disable it

Kernel cmdline (`strings boot.img`) has **no `androidboot.selinux=enforcing`**;
build is `userdebug`; and `init.rc:359-361` carries the stock KitKat block:

```
# Set these so we can remotely update SELinux policy
    chown system system /sys/fs/selinux/load
    chown system system /sys/fs/selinux/enforce
```

Because `MifiService` runs as **uid=system** (see #1), the very process exposing
the unauthenticated `/ajax` API owns `/sys/fs/selinux/enforce` and
`/sys/fs/selinux/load` — it (or any system-uid code) can set SELinux permissive or
load a replacement policy. Combined with `ro.secure=0`, the platform provides no
reliable MAC confinement for the web backend. (Stock-KitKat line, not
vendor-added, but security-relevant on this config.)

### D6. LOW / informational — single C2 confirmed; leftover WebSocket demo page

- Decompiled + strings-scanned **all 59 APKs** in `/system/app` + `/system/priv-app`:
  the only non-stock external endpoint anywhere is **`120.77.62.229:8080`**
  (MifiService). Other externals are benign stock: `221.176.0.11/12/55` (China
  Mobile AGPS/SUPL in `AGPSTestMode.apk`), `qrd*.qualcomm.com` (`Logkit.apk`),
  `se.cgbe.trustonic.com` (`RootPA.apk` TEE provisioning), Google captive-portal
  (`Settings.apk`). No second phone-home / C2 channel exists.
- `assets/jetty2/chat.html` opens `ws://<host>/wschat`, but `JServer.start()`
  registers only the `/ajax` servlet + a static `ResourceHandler` (no `/wschat`
  WebSocket endpoint) — a **dead leftover Jetty demo asset**, not a live listener.
  Confirms the web server binds only port 80 on `0.0.0.0` (`new Server(mPort)`,
  `mPort=80`), serving resource base `jetty2` extracted to the app's `filesDir`.

### Confirmations / refined non-findings (no new exposure)

- **No command injection into the `su`/shell helper from the network.** androguard
  xref: `ShellUtils.execCommand` (which pipes to `su`) is called **only** by
  `ShellUtils.checkRootPermission`; `WanDataController.doShellCommand` only by
  `WanDataController$ChangeSimTask`. Neither is reachable from `AjaxSevlet.doPost`.
  The `/ajax`-reachable `Runtime.exec(String)` calls (`setWlanDns`, `setApnByAt`,
  `setPreferedNetworkType`, `sendAtBySmd`, `restoreDevice`, the funcNo-2001
  `setprop`) all use the single-string form (no shell) → argument injection at
  most, as the first pass stated.
- **No additional local-IPC attack surface.** `MifiService`'s only
  intent-filtered (implicitly exported) receivers handle the *protected* system
  broadcasts `BOOT_COMPLETED` and `ACTION_SHUTDOWN`; `MainActivity` is the launcher
  activity. The dangerous operations (OTA flash, factory reset, IMEI write, ADB
  enable) are reachable **only** through the network `/ajax` servlet, not via any
  app-to-app broadcast/intent. Manifest confirms `sharedUserId=android.uid.system`
  and the heavy permission set (`MASTER_CLEAR`, `WRITE_SECURE_SETTINGS`,
  `MOUNT_UNMOUNT_FILESYSTEMS`, `DEVICE_POWER`, `MODIFY_PHONE_STATE`, …).
- **No private keys / credentials in `system` or `persist`.** The only key-like
  file is `/system/lib/crda/linville.key.pub.pem` (public wireless-regdb key).
  `persist` holds only `wifi.json` (`{"WIFIssid":"4G-UFI-","WIFIpassword":"1234567890"}`
  — the same default WPA key as #11, now confirmed at rest on the `persist`
  partition), `wcnss_mac_addr` (`A0:2B:2C:XX:XX:XX`), `WCNSS_qcom_wlan_nv.bin`,
  `.bt_nv.bin`. `system/etc/hostapd/hostapd_default.conf` and
  `wifi/wpa_supplicant.conf` are templates with all PSK lines commented out. No
  `/etc/passwd`/`shadow`, no `authorized_keys`, no `dropbear`/ssh host keys.
- **No extra boot-time network listeners.** Reviewed every `init*.rc`: the only
  services opening sockets are stock Qualcomm daemons on **UNIX/local** sockets
  (`cnd`, `dpmd`, `rild1/2`, `ims*`, `ppd`, `wpa_supplicant`) — none bind a TCP/IP
  port. No `telnetd`/`dropbear`/`ssh`/`netcat`/`inetd` service is defined. The sole
  vendor-added boot service is `initmifiservice` (`init.target.rc`, runs as **root**
  at `sys.boot_completed=1`): it sets the USB composition, loads `xt_HL.ko`, adds
  the TTL=64 mangle rules (tether-hiding), builds bridge **`br0` = 192.168.100.1
  over `rndis0`**, runs `modem_at`, and `chmod 666 /dev/smd11` (per #12). adbd is
  started via `init.usb.rc` on `sys.usb.config=adb` (default), confirming #3.
- `modem.img` (FAT) and `misc.img` contained no credentials, OTA URL, or
  recovery-command strings of interest.
