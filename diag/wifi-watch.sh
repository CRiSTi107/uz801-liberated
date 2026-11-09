#!/system/bin/sh
# wifi-watch.sh — read-only watchdog that logs WiFi-AP health to catch the
# "hotspot drops" the owner reported. It only READS state; it never restarts
# anything (and NEVER touches adbd). Safe to leave running for a soak test.
#
# Usage (from the PC):
#   adb -s 0123456789ABCDEF push diag/wifi-watch.sh /data/linux/
#   adb -s 0123456789ABCDEF shell "/system/bin/sh /data/linux/wifi-watch.sh [interval_s] [logfile]"
# Default: every 5 s → /data/linux/wifi-watch.log (+ stdout). Ctrl-C to stop.
#
# Logs: hostapd pid, wlan0 carrier/operstate, bridge membership, #clients,
# loadavg, SoC temp. Prints a DROP/RECOVER marker on any AP-down transition so
# you can line it up against CPU/thermal/traffic.

INTERVAL="${1:-5}"
LOG="${2:-/data/linux/wifi-watch.log}"
IF="${WIFI_IF:-wlan0}"
LEASES=/data/misc/dhcp/dnsmasq.leases

read1() { busybox cat "$1" 2>/dev/null; }

echo "# wifi-watch start $(busybox date 2>/dev/null) if=$IF interval=${INTERVAL}s" | busybox tee -a "$LOG"
PREV_UP=""
while :; do
  ts=$(busybox date '+%H:%M:%S' 2>/dev/null)

  hpid=$(busybox pgrep hostapd 2>/dev/null | busybox head -1)
  oper=$(read1 /sys/class/net/$IF/operstate)
  carrier=$(read1 /sys/class/net/$IF/carrier)       # 1=up 0=down
  inbr=$( [ -e /sys/class/net/$IF/brport ] && echo yes || echo NO )
  clients=$(busybox awk 'END{print NR}' "$LEASES" 2>/dev/null); [ -z "$clients" ] && clients=0
  load=$(read1 /proc/loadavg | busybox cut -d' ' -f1)
  # SoC temperature (first thermal zone that reports millidegrees)
  temp="--"; for z in /sys/class/thermal/thermal_zone*/temp; do
    t=$(read1 "$z"); [ -n "$t" ] && { [ "$t" -gt 1000 ] 2>/dev/null && t=$((t/1000)); temp="${t}C"; break; }
  done

  up=1; [ -z "$hpid" ] && up=0; [ "$carrier" = "0" ] && up=0; [ "$oper" = "down" ] && up=0
  line="$ts  hostapd=${hpid:-DEAD} $IF=oper:${oper:-?}/carrier:${carrier:-?} inbridge:$inbr clients=$clients load=$load temp=$temp"

  mark=""
  if [ "$up" != "$PREV_UP" ] && [ -n "$PREV_UP" ]; then
    [ "$up" = 0 ] && mark="   <<< AP DROP" || mark="   >>> AP RECOVER"
  fi
  echo "$line$mark" | busybox tee -a "$LOG"
  PREV_UP="$up"
  busybox sleep "$INTERVAL"
done
