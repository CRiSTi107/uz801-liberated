# Documentation index

Full documentation for the UZ801 dongle project. Start at the
[root README](../README.md) for the overview, the
[security report](../REPORT.md) for the consolidated write-up, or
[PROGRESS.md](../PROGRESS.md) for the current state and next actions.

> All device/SIM identifiers (IMEI/IMSI/ICCID/MAC/SSID) in these docs are **redacted**
> to masked placeholders (`…XXXXX`). See the root README's "Privacy & redaction" note.

## Security
| Doc | What's in it |
|-----|--------------|
| [security/SECURITY-AUDIT.md](security/SECURITY-AUDIT.md) | Full static firmware audit of the dumped images — unauthenticated `/ajax`, ADB-root-by-default, unsigned OTA, cleartext phone-home. Analysis only, nothing executed. |
| [security/EXPLOITS.md](security/EXPLOITS.md) | Backdoors & exploits catalog with PoCs **and mitigation status** (what's fixed on this unit). |
| [security/IP-INVESTIGATION.md](security/IP-INVESTIGATION.md) | Deep-dive on the `120.77.62.229` firmware phone-home / OTA channel. |

## Hardware
| Doc | What's in it |
|-----|--------------|
| [hardware/SPECS.md](hardware/SPECS.md) | Hardware & platform specs (MSM8916, RAM/eMMC, WiFi/BT, USB, LTE bands). |
| [hardware/CAPABILITIES.md](hardware/CAPABILITIES.md) | Band capabilities → which carriers/bands actually work. |
| [hardware/ANTENNA.md](hardware/ANTENNA.md) | External LTE antenna — what to buy, dipole leg lengths, how to fit and test it. |

## Plans & build notes
| Doc | What's in it |
|-----|--------------|
| [plans/COCKPIT-API-PLAN.md](plans/COCKPIT-API-PLAN.md) | Design of the authenticated cockpit API + dashboard that replaces the stock backdoor UI. |
| [plans/CHROOT-LINUX-PLAN.md](plans/CHROOT-LINUX-PLAN.md) | Alpine/Debian chroot on the dongle with real root (the stack the cockpit runs in). |
| [plans/BANDLOCK-LINUX-PLAN.md](plans/BANDLOCK-LINUX-PLAN.md) | On-device LTE band-lock execution plan (DIAG/EFS vs. QMI-over-qmuxd routes). |
| [plans/LINUX-OPTIONS.md](plans/LINUX-OPTIONS.md) | Viability assessment for flashing a mainline Linux distro (OpenStick/OpenWrt). |

## History
| Doc | What's in it |
|-----|--------------|
| [INVESTIGATION-LOG.md](INVESTIGATION-LOG.md) | The full chronological investigation log + dated **Status Log** (the original README). |
| [SESSION-HISTORY.md](SESSION-HISTORY.md) | Condensed per-session history. |

## Code & scripts (not under docs/)
| Path | What's there |
|------|--------------|
| [`../cockpit/api/`](../cockpit/api/) | Python-stdlib HTTPS cockpit API (auth, TLS, modem AT, QMI band-lock, allow-listed stock proxy). See its [README](../cockpit/api/README.md). |
| [`../cockpit/web/`](../cockpit/web/) | Cockpit single-page web UI. See its [README](../cockpit/web/README.md). |
| [`../webui/`](../webui/) | Earlier on-device web UI + device deploy scripts (`harden.sh`, `boot-hook.sh`, `start-webui.sh`). |
| [`../alpine/`](../alpine/) | Chroot scaffolding (`enter.sh`, `start-services.sh`). |
| [`../diag/`](../diag/) | QMI/DIAG probes and band/antenna test tools (`uztest.sh`, `qmi_*.py`, `test_*.py`). |
| [`../host/`](../host/) | PC-side helpers (host routing setup). |
| [`../dump/`](../dump/) | Firmware dump **text records** (partition map, md5 manifest, logs, GPT). The 953 MB `.img` blobs are gitignored. |
