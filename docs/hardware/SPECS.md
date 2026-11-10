# UZ801 4G USB Dongle — Hardware & Platform Specs

Read live from the device (2026-11-07) via `getprop`, `/proc`, `/sys`, `lsusb`.

## Identity
| | |
|---|---|
| Product | **UZ801** 4G UFI (WiFi + USB MiFi dongle), fw `UZ801-V2.3.12` |
| Board / device | `ALK` / `msm8916_32_512` |
| Manufacturer (prop) | Qualcomm Technology |
| IMEI | `8600180460XXXXX` |
| Serial (`ro.serialno`) | `a164ae`  ·  USB ADB serial `0123456789ABCDEF` |
| Power | USB bus-powered |

## SoC
| | |
|---|---|
| SoC | **Qualcomm Snapdragon 410 / MSM8916** (`ro.board.platform=msm8916`, `ro.hardware=qcom`) |
| CPU | ARM **Cortex-A53** (ARMv7l, 32-bit userspace), `armeabi-v7a` |
| Cores online | **2** (`/sys/devices/system/cpu/present = 0-1`) — this unit exposes 2 of the MSM8916's cores |
| CPU freq | **200 MHz – 1.1904 GHz** (`scaling_min 200000` / `cpuinfo_max 1190400` kHz) |
| CPU features | neon, vfpv3, vfpv4, idiva, idivt, thumb, fastmult (BogoMIPS 38.40) |
| GPU | **Qualcomm Adreno 306**, ~**310 MHz** (`/sys/class/kgsl/kgsl-3d0/gpuclk=310000000`) |
| GPU API | OpenGL **ES 3.0** (`ro.opengles.version=196608`, `debug.egl.hw=1`) |

## Memory & storage
| | |
|---|---|
| RAM (physical) | **512 MB** |
| RAM (MemTotal) | **397,824 kB** (~388 MiB usable; rest reserved for radio/GPU) |
| Swap (zram) | **196,604 kB** (~192 MB) |
| eMMC total | **~4 GB** (`mmcblk0` = 7,634,944 × 512 B = **3.64 GiB / 3.91 GB**) |

### Partitions (mounted)
| Mount | Device | Size | FS | Notes |
|---|---|---|---|---|
| `/system` | `by-name/system` | 774.9 M | ext4 | **ro** (remountable, persistent) |
| `/data` | `by-name/userdata` | 774.9 M | ext4 | rw, ~700 M free |
| `/cache` | `by-name/cache` | 122.0 M | ext4 | rw |
| `/persist` | `by-name/persist` | 27.5 M | ext4 | rw (wifi.json, wcnss mac, BT/WLAN nv) |

(25 partitions total incl. boot/recovery/modem/aboot/sbl1/rpm/tz/… — see `dump/by-name.txt`.)

## Modem / RF
| | |
|---|---|
| Modem | Integrated MSM8916 LTE (Gobi-class), `ro.baseband=msm` |
| Category | LTE Cat 4 (per vendor/community) |
| LTE bands | **FDD B1 / B3 / B5 / B8 + TDD B40** (no B7 / B20 / B28) |
| Cellular iface | `rmnet0` (carrier WAN) |
| AT access | `/dev/smd11` (0666) via on-device `modem_at "AT+..."`; `AT+CGSN` read, `AT+WRIMEI=` write |

## WiFi / Bluetooth
| | |
|---|---|
| WLAN/BT chip | Qualcomm **WCN3620** (WLAN MAC `a0:2b:2c:XX:XX:XX`) |
| WiFi | 802.11 b/g/n, 2.4 GHz, mode `n`, up to 10 clients (hostapd AP) |
| Default SSID | `4G-UFI-XXX` (template `4G MIFI-DA7` / `4G-UFI-<mac3>`) |
| Bluetooth | present (ath3k/qca BT init scripts) |

## OS / kernel
| | |
|---|---|
| Android | **4.4.4 KitKat** (API/SDK **19**) |
| Build | `eng..20240812` · `userdebug` · **test-keys** · dated 2024-08-12 |
| Fingerprint | `qcom/msm8916_32_512/msm8916_32_512:4.4.4/KTU84P/eng..20240812:userdebug/test-keys` |
| Kernel | **Linux 3.10.28** SMP PREEMPT, armv7l, gcc 4.7 (built 2024-08-12) |
| SELinux | Enforcing (but `userdebug` + system-uid can relax — see SECURITY-AUDIT D5) |

## USB
| | |
|---|---|
| Normal mode | `05c6:f00e` (Qualcomm) — presents RNDIS NIC |
| ADB/debug mode | `05c6:90b6` (Qualcomm, Android) |
| USB composite | `rndis,serial_smd,diag,adb` (ADB + RNDIS share **one** gadget) |

## Networking layout
- Bridge **`br0` = `192.168.100.1/24`** holds **`wlan0` + `rndis0`** (WiFi and USB clients share the subnet).
- `rmnet0` = cellular WAN (e.g. `10.74.88.195/29` when attached).
- dnsmasq serves DHCP/DNS on `br0`; admin/API on `:80`; custom UI on `:8088`.
