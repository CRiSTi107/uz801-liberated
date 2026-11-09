#!/system/bin/sh
# signal-monitor.sh — read-only LTE registration / band / signal monitor.
# Runs on the dongle (busybox sh), talks to the modem via `modem_at` (/dev/smd11).
# It NEVER writes anything to the modem — purely diagnostic. Safe to leave running.
#
# Usage (from the PC):
#   adb -s 0123456789ABCDEF push diag/signal-monitor.sh /data/linux/
#   adb -s 0123456789ABCDEF shell "/system/bin/sh /data/linux/signal-monitor.sh [interval_s] [logfile]"
# Default: every 5 s, log to /data/linux/signal-monitor.log (and stdout).
# Stop with Ctrl-C (or `adb shell busybox pkill -f signal-monitor`).
#
# Columns: time  CEREG  CGATT  CSQ(dBm)  bestUsableCell(band earfcn rsrp)  allCells  [CEER on denied]

INTERVAL="${1:-5}"
LOG="${2:-/data/linux/signal-monitor.log}"

at() { modem_at "$1" 2>/dev/null | busybox grep -vE '^main,|^\s*$'; }

# EARFCN -> E-UTRA band + usable-on-this-hardware flag.
# Supported FDD: B1 B3 B5 B8 ; TDD: B40.  Unusable here: B7 B20 B38 (+ unknown).
earfcn_band() {  # echoes "<band> <usable 1/0>"
  e="$1"
  if   [ "$e" -ge 0 ]     && [ "$e" -le 599 ];   then echo "B1 1"
  elif [ "$e" -ge 1200 ]  && [ "$e" -le 1949 ];  then echo "B3 1"
  elif [ "$e" -ge 2400 ]  && [ "$e" -le 2649 ];  then echo "B5 1"
  elif [ "$e" -ge 2750 ]  && [ "$e" -le 3449 ];  then echo "B7 0"
  elif [ "$e" -ge 3450 ]  && [ "$e" -le 3799 ];  then echo "B8 1"
  elif [ "$e" -ge 6150 ]  && [ "$e" -le 6449 ];  then echo "B20 0"
  elif [ "$e" -ge 37750 ] && [ "$e" -le 38249 ]; then echo "B38 0"
  elif [ "$e" -ge 38650 ] && [ "$e" -le 39649 ]; then echo "B40 1"
  else echo "B? 0"; fi
}

csq_dbm() { c="$1"; [ -z "$c" ] && { echo "--"; return; }
  [ "$c" = "99" ] && { echo "99/unk"; return; }
  echo "$(( -113 + 2 * c ))"; }

echo "# signal-monitor start $(busybox date 2>/dev/null) interval=${INTERVAL}s" | busybox tee -a "$LOG"
PREV_STATE=""
while :; do
  ts=$(busybox date '+%H:%M:%S' 2>/dev/null)
  cereg=$(at 'AT+CEREG?' | busybox sed -n 's/.*+CEREG: *[0-9],\([0-9]\).*/\1/p' | busybox head -1)
  cgatt=$(at 'AT+CGATT?' | busybox sed -n 's/.*+CGATT: *\([0-9]\).*/\1/p' | busybox head -1)
  csq=$(at 'AT+CSQ'     | busybox sed -n 's/.*+CSQ: *\([0-9]*\),.*/\1/p' | busybox head -1)
  qc=$(at 'AT$QCRSRP?'  | busybox sed -n 's/.*\$QCRSRP: *//p' | busybox head -1)

  # Parse the QCRSRP triplets: pci,earfcn,rsrp,...  find the best USABLE-band cell.
  best=""; bestrsrp=-999; all=""
  set -- $(echo "$qc" | busybox tr ',"' '  ')
  while [ $# -ge 3 ]; do
    earf="$2"; rsrp="$3"; shift 3
    case "$earf" in ''|*[!0-9]*) continue;; esac
    bu=$(earfcn_band "$earf"); band=${bu% *}; usable=${bu#* }
    ri=$(echo "$rsrp" | busybox cut -d. -f1 | busybox tr -d '+-' ); rv="-$ri"
    all="$all ${band}:${earf}@${rv}$( [ "$usable" = 0 ] && echo '!' )"
    # ignore sentinel/unusable values; track best usable
    if [ "$usable" = 1 ] && [ "$ri" -lt 140 ] && [ "$ri" -gt 20 ]; then
      if [ "$rv" -gt "$bestrsrp" ]; then bestrsrp="$rv"; best="${band} ${earf} ${rv}dBm"; fi
    fi
  done
  [ -z "$best" ] && best="(no usable cell)"

  reg="$cereg"; case "$reg" in 1) reg="1-home";; 5) reg="5-roam";; 2) reg="2-search";; 3) reg="3-DENIED";; 0) reg="0-none";; esac
  line="$ts  CEREG=$reg CGATT=${cgatt:-?} CSQ=$(csq_dbm "$csq")dBm  best=[$best]  cells:$all"
  echo "$line" | busybox tee -a "$LOG"

  # On a denied/failed state transition, grab the extended error once.
  state="$reg/$cgatt"
  if [ "$state" != "$PREV_STATE" ]; then
    case "$reg" in *DENIED*|2-search) ceer=$(at 'AT+CEER' | busybox tr '\n' ' '); echo "    ^ transition; CEER: $ceer" | busybox tee -a "$LOG";; esac
    PREV_STATE="$state"
  fi
  busybox sleep "$INTERVAL"
done
