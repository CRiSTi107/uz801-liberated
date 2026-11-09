#!/system/bin/sh
# uz-netservices.sh - take over the UZ801 MiFi network plumbing so the stock
# MiFi app (com.mifiservice.hello) can eventually be DELETED.
#
# This script makes the cockpit self-sufficient for the WAN/LAN/WiFi/DHCP/NAT
# plumbing that the stock app (and the Android init/netd stack) currently
# provide. It is DEFENSIVE and IDEMPOTENT: every step is guarded and logs what
# it did, nothing tears down working state, and it is SAFE TO RUN while the
# stock app and netd are still up. Run it as root, on the device (host
# Android side, busybox sh), NOT inside the Alpine chroot.
#
#   adb shell /system/bin/sh /data/.../uz-netservices.sh
#
# ---------------------------------------------------------------------------
# WHO PROVIDES WHAT TODAY (so you know what this script duplicates / adds):
#
#   * Bridge br0 = 192.168.100.1/24 over wlan0 + rndis0 ....... initmifiservice.sh
#     (a BOOT SCRIPT - KEEP IT. It also `chmod 666 /dev/smd11` and preserves
#      our persist.sys.usb.config adb value.) This script does NOT build the
#      bridge or assign the LAN IP; it only verifies they exist.
#   * ip_forward=1 + MASQUERADE -o rmnet0 ..................... netd (init-started)
#   * hostapd (-e entropy.bin hostapd.conf) .................. child of netd
#   * dnsmasq (DHCP/DNS for br0) ............................. child of netd
#   * The cellular data PDP context (dial) .................. RIL / stock app
#
# netd normally (re)launches hostapd and dnsmasq. The steps below are written
# so that IF netd/the stock app are present they are a no-op (already-running
# checks short-circuit), and IF the stock app has been removed they bring the
# same services up themselves. The ONLY genuinely new piece is the AT-based
# data dial fallback (step 6), used only if nothing else has activated the PDP.
# ---------------------------------------------------------------------------

# ---- configuration (override via environment) -----------------------------
WAN_IFACE="${WAN_IFACE:-rmnet0}"
LAN_IFACE="${LAN_IFACE:-br0}"
LAN_IP="${LAN_IP:-192.168.100.1}"
DHCP_START="${DHCP_START:-192.168.100.100}"
DHCP_END="${DHCP_END:-192.168.100.200}"
DHCP_LEASE="${DHCP_LEASE:-12h}"

HOSTAPD_BIN="${HOSTAPD_BIN:-/system/bin/hostapd}"
HOSTAPD_CONF="${HOSTAPD_CONF:-/data/misc/wifi/hostapd.conf}"
HOSTAPD_ENTROPY="${HOSTAPD_ENTROPY:-/data/misc/wifi/entropy.bin}"

DNSMASQ_BIN="${DNSMASQ_BIN:-/system/bin/dnsmasq}"
DNSMASQ_LEASES="${DNSMASQ_LEASES:-/data/misc/dhcp/dnsmasq.leases}"

IPTABLES="${IPTABLES:-/system/bin/iptables}"
NDC="${NDC:-/system/bin/ndc}"    # netd client - drives SoftAP + tethering like the framework
AP_IFACE="${AP_IFACE:-wlan0}"
SMD="${SMD:-/dev/smd11}"
APN="${APN:-internet}"          # data APN to dial as a last-resort fallback
PDP_CID="${PDP_CID:-1}"

log() { echo "[uz-netservices] $*"; }
have() { [ -x "$1" ] || command -v "$1" >/dev/null 2>&1; }
# Portable "is process running?" — the Android host shell lacks pgrep/pidof,
# so go through busybox (the [x]yz trick avoids self-matching the grep).
proc_running() {
  busybox pgrep -x "$1" >/dev/null 2>&1 && return 0
  p="[${1%"${1#?}"}]${1#?}"
  busybox ps -o args 2>/dev/null | busybox grep -qE "$p"
}
# Run an ndc command and log each line of its reply (prefixed).
ndcrun() { "$NDC" "$@" 2>&1 | while IFS= read -r _l; do log "  ndc: $_l"; done; }
# Read a key=value from the hostapd.conf (first match).
hconf() { busybox grep -m1 "^$1=" "$HOSTAPD_CONF" 2>/dev/null | busybox cut -d= -f2-; }

log "starting (WAN=$WAN_IFACE LAN=$LAN_IFACE LAN_IP=$LAN_IP)"

# ===========================================================================
# 0. Sanity: the bridge + LAN IP come from initmifiservice.sh. Only verify.
# ===========================================================================
if ip addr show "$LAN_IFACE" 2>/dev/null | grep -q "$LAN_IP"; then
  log "OK   [init] $LAN_IFACE has $LAN_IP (from initmifiservice.sh)"
else
  log "WARN [init] $LAN_IFACE / $LAN_IP not found. The bridge is built by"
  log "WARN        initmifiservice.sh at boot - this script does NOT create it."
  log "WARN        Continuing; hostapd/dnsmasq below still configured."
fi

# ===========================================================================
# 1. ip_forward=1   (normally set by netd - ensure it, harmless if already 1)
# ===========================================================================
CUR_FWD="$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)"
if [ "$CUR_FWD" = "1" ]; then
  log "OK   [netd] ip_forward already 1"
else
  echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null \
    && log "SET  ip_forward=1" \
    || log "FAIL could not set ip_forward (need root?)"
fi

# ===========================================================================
# 2. NAT: MASQUERADE out of the cellular WAN (normally a netd rule). Add only
#    if an equivalent rule is not already present, in BOTH the vendor netd
#    chain (natctrl_nat_POSTROUTING) and the standard POSTROUTING, whichever
#    exists - checked idempotently so we never duplicate a working rule.
# ===========================================================================
nat_rule_present() {
  # $1 = chain
  "$IPTABLES" -t nat -S "$1" 2>/dev/null | \
    grep -q -- "-o $WAN_IFACE -j MASQUERADE"
}
if have "$IPTABLES"; then
  if nat_rule_present natctrl_nat_POSTROUTING; then
    log "OK   [netd] MASQUERADE -o $WAN_IFACE already in natctrl_nat_POSTROUTING"
  elif nat_rule_present POSTROUTING; then
    log "OK   [netd] MASQUERADE -o $WAN_IFACE already in POSTROUTING"
  else
    # Prefer the vendor chain if it exists, else fall back to POSTROUTING.
    if "$IPTABLES" -t nat -S natctrl_nat_POSTROUTING >/dev/null 2>&1; then
      "$IPTABLES" -t nat -A natctrl_nat_POSTROUTING -o "$WAN_IFACE" \
        -j MASQUERADE 2>/dev/null \
        && log "ADD  MASQUERADE -o $WAN_IFACE (natctrl_nat_POSTROUTING)" \
        || log "FAIL adding MASQUERADE to natctrl_nat_POSTROUTING"
    else
      "$IPTABLES" -t nat -A POSTROUTING -o "$WAN_IFACE" -j MASQUERADE \
        2>/dev/null \
        && log "ADD  MASQUERADE -o $WAN_IFACE (POSTROUTING)" \
        || log "FAIL adding MASQUERADE to POSTROUTING"
    fi
  fi
else
  log "WARN iptables not found at $IPTABLES - skipping NAT step"
fi

# ===========================================================================
# 3. WiFi AP via netd (`ndc softap`).
#    STATUS (tested 2026-11-09, app disabled + reboot): PARTIAL. `ndc softap
#    set/startap` run and hostapd comes up, BUT the AP is not usable by wireless
#    clients on its own — the stock app/netd also LOAD the WLAN kernel driver in
#    AP mode (so /sys/module/wlan/parameters/fwpath=ap) and bridge wlan0 into
#    br0, and our standalone `ndc softap fwreload` fails at boot (driver not
#    loaded → fwpath missing). So WiFi-client association does NOT work with the
#    stock app removed yet. The `ndc tether` / dnsmasq path (step 4) DOES work.
#    TODO before the app can be deleted: replicate the WLAN-driver AP-mode load
#    + wlan0→br0 bridge (the hard, driver-level part).
#    A bare `hostapd -B` is NOT enough: the
#    WCN3620 must first be switched to AP-mode firmware (/sys/module/wlan/
#    parameters/fwpath=ap) and the interface put in AP mode - that is netd's
#    job, driven by the framework. We replicate exactly what the stock app
#    triggers: fwreload AP -> set (ssid/hidden/channel/security/key, read from
#    the existing hostapd.conf) -> startap. Idempotent: skip if hostapd is up.
# ===========================================================================
if proc_running hostapd; then
  log "OK   [netd] hostapd already running - not touching it"
elif have "$NDC"; then
  ap_ssid="$(hconf ssid)"; ap_chan="$(hconf channel)"; ap_pass="$(hconf wpa_passphrase)"
  [ "$(hconf wpa)" = "2" ] && ap_sec="wpa2-psk" || { [ -n "$ap_pass" ] && ap_sec="wpa2-psk" || ap_sec="open"; }
  [ "$(hconf ignore_broadcast_ssid)" = "1" ] && ap_bcast="hidden" || ap_bcast="broadcast"
  [ -z "$ap_chan" ] && ap_chan="6"
  log "SOFTAP fwreload $AP_IFACE AP; set '$ap_ssid' $ap_bcast ch$ap_chan $ap_sec; startap"
  ndcrun softap fwreload "$AP_IFACE" AP
  if [ "$ap_sec" = "open" ]; then
    ndcrun softap set "$AP_IFACE" "$ap_ssid" "$ap_bcast" "$ap_chan" open
  else
    ndcrun softap set "$AP_IFACE" "$ap_ssid" "$ap_bcast" "$ap_chan" "$ap_sec" "$ap_pass"
  fi
  ndcrun softap startap
  sleep 2
  proc_running hostapd && log "OK   AP up via ndc softap" || log "FAIL AP did not come up via ndc softap"
else
  log "WARN ndc not found at $NDC - cannot start SoftAP"
fi

# ===========================================================================
# 4. DHCP/DNS via netd (`ndc tether`). netd launches dnsmasq for the tethered
#    LAN with the given range. Idempotent: skip if dnsmasq is already up.
# ===========================================================================
if proc_running dnsmasq; then
  log "OK   [netd] dnsmasq already running - not touching it"
elif have "$NDC"; then
  mkdir -p "${DNSMASQ_LEASES%/*}" 2>/dev/null
  log "TETHER start $DHCP_START $DHCP_END; interface add $LAN_IFACE"
  ndcrun tether start "$DHCP_START" "$DHCP_END"
  ndcrun tether interface add "$LAN_IFACE"
  sleep 2
  proc_running dnsmasq && log "OK   dnsmasq up via ndc tether" || log "FAIL dnsmasq did not come up via ndc tether"
else
  log "WARN ndc not found at $NDC - cannot start DHCP"
fi

# ===========================================================================
# 5. Data PDP context (dial) - FALLBACK ONLY. The RIL / stock app normally
#    activates data. We only dial if the WAN iface has no IPv4 yet. Uses the
#    modem AT channel directly (/dev/smd11), mirroring modem.py.
# ===========================================================================
wan_has_ip() {
  ip addr show "$WAN_IFACE" 2>/dev/null | grep -q "inet "
}
at() {
  # usage: at "AT+CMD"   - write to smd11, read best-effort reply (busybox).
  [ -c "$SMD" ] || { log "WARN $SMD not a char device - cannot dial"; return 1; }
  busybox printf '%s\r' "$1" > "$SMD" 2>/dev/null
  # Best-effort short read so the modem has time to process; non-fatal.
  busybox dd if="$SMD" bs=256 count=1 iflag=nonblock 2>/dev/null | busybox tr -d "\0"
  return 0
}
if wan_has_ip; then
  log "OK   [ril] $WAN_IFACE already has an IPv4 - not dialling"
else
  if [ -c "$SMD" ]; then
    log "DIAL $WAN_IFACE has no IP - defining + activating PDP (APN=$APN)"
    at "AT+CGDCONT=$PDP_CID,\"IP\",\"$APN\"" >/dev/null 2>&1
    at "AT+CGACT=1,$PDP_CID" >/dev/null 2>&1
    # Give the stack a moment, then report.
    sleep 3
    if wan_has_ip; then
      log "OK   PDP activated - $WAN_IFACE now has an IPv4"
    else
      log "WARN PDP dial issued but $WAN_IFACE still has no IPv4 (band/registration?)"
    fi
  else
    log "WARN $SMD missing - cannot dial data; leaving to RIL"
  fi
fi

log "done. (idempotent - safe to re-run)"
exit 0
