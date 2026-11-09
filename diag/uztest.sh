#!/usr/bin/env bash
# uztest.sh — laptop-side helper to drive the UZ801 modem band/data tests
# over adb (no need to type the long enter.sh/python invocations).
#
# The dongle's QMI channel (/dev/smdcntl8) is created by enter.sh; this script
# also re-creates it defensively before each run.
#
# Usage:
#   ./diag/uztest.sh auto            # unlock all bands + 2G/3G/4G, watch ~60s
#   ./diag/uztest.sh band B8         # lock ONE band, watch reg/rssi/data ~40s
#   ./diag/uztest.sh data            # per-band data sweep (all bands incl B20)
#   ./diag/uztest.sh netscan         # reg state / serving PLMN / RSSI snapshot
#   ./diag/uztest.sh read            # current band-lock state
#   ./diag/uztest.sh lock B8         # lock to a band (B1/B3/B5/B7/B8/B20/B40)
#   ./diag/uztest.sh restore         # lock back to B1/B3/B5/B8/B40
#
# Device serial defaults to the UZ801; override with:  UZ_SERIAL=XXXX ./uztest.sh ...
set -euo pipefail

SERIAL="${UZ_SERIAL:-0123456789ABCDEF}"
ADB=(adb -s "$SERIAL")
# ensures the free QMI control node exists in the chroot before each command
PRE='for m in 8 9; do mknod /dev/smdcntl$m c 244 $m 2>/dev/null; done;'

run() {   # run a command string inside the Alpine chroot
  "${ADB[@]}" shell "/system/bin/sh /data/linux/enter.sh '$PRE $*'"
}

mask_for() {
  case "${1^^}" in
    B1)  echo 0x1 ;;
    B3)  echo 0x4 ;;
    B5)  echo 0x10 ;;
    B7)  echo 0x40 ;;
    B8)  echo 0x80 ;;
    B20) echo 0x80000 ;;
    B40) echo 0x8000000000 ;;
    AUTO|USABLE) echo 0x0000008000000095 ;;
    0X*|0x*) echo "$1" ;;
    *) return 1 ;;
  esac
}

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

cmd="${1:-help}"; shift || true
case "$cmd" in
  auto)    run python3 /opt/diag/test_auto.py ;;
  data)    run python3 /opt/diag/data_band_test.py ;;
  netscan) run python3 /opt/diag/qmi_netscan.py ;;
  read)    run python3 /opt/cockpit/api/qmi.py read ;;
  band)    run python3 /opt/diag/test_band.py "${1:-B20}" ;;
  lock)
    [ $# -ge 1 ] || { echo "usage: uztest.sh lock <band|0xmask>"; exit 2; }
    m="$(mask_for "$1")" || { echo "unknown band: $1"; exit 2; }
    run python3 /opt/cockpit/api/qmi.py apply "$m" ;;
  restore) run python3 /opt/cockpit/api/qmi.py apply 0x0000008000000095 ;;
  help|-h|--help) usage ;;
  *) echo "unknown command: $cmd"; echo; usage; exit 2 ;;
esac
