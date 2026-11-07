#!/system/bin/sh
# ======================================================================
# UZ801 hardening (policy: KEEP WiFi, secure by password) — close the
# REMOTE/internet surface and the phone-home, keep LOCAL root.
#
# SAFETY: touches ONLY iptables. Never restarts adbd or changes USB/adb
# props (that cycles the shared rndis+adb USB gadget and drops USB adb).
# Network adb (7628) is already closed via persisted persist.adb.tcp.port=-1;
# we also firewall 7628 as defense-in-depth so it can't be re-opened remotely.
#
# The stock unauth API (:80) and old shim (:8088) are confined to localhost so
# the Chinese backdoor is not reachable from WiFi/USB/cellular; the authenticated
# cockpit (:443) is the only exposed admin interface and proxies :80 over lo.
#
# Revert with unharden.sh.
# ======================================================================

PHONEHOME="120.77.62.229"

iptables -N UZSEC 2>/dev/null
iptables -F UZSEC
iptables -C INPUT -j UZSEC 2>/dev/null || iptables -I INPUT 1 -j UZSEC

iptables -A UZSEC -i lo -j RETURN
iptables -A UZSEC -m state --state ESTABLISHED,RELATED -j RETURN
# Internet-facing cellular interface: drop ALL new inbound (closes 80/8088/DNS/etc to the internet).
iptables -A UZSEC -i rmnet0 -j DROP
# Network adb: never allow new inbound to 7628 from anywhere but loopback.
iptables -A UZSEC -p tcp --dport 7628 -j DROP
# DECOMMISSION the Chinese backdoor: the stock unauth API (:80) and the old
# unauth shim (:8088) are now reachable ONLY from localhost (the authenticated
# cockpit on :443 proxies to :80 via lo, which RETURNs above). WiFi/USB/cellular
# clients can no longer reach the unauthenticated API at all.
iptables -A UZSEC -p tcp --dport 80   -j DROP
iptables -A UZSEC -p tcp --dport 8088 -j DROP

# Phone-home / server-controlled remote-flash channel (outbound).
iptables -C OUTPUT -d $PHONEHOME -j DROP 2>/dev/null || iptables -A OUTPUT -d $PHONEHOME -j DROP

echo "[harden] INPUT chain UZSEC:"
iptables -L UZSEC -n -v
echo "[harden] OUTPUT phone-home:"
iptables -L OUTPUT -n | busybox grep -i "$PHONEHOME"
echo "[harden] done (adbd/USB/WiFi untouched; LAN admin left open per policy)."
