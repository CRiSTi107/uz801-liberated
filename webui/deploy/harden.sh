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
# What it does NOT do: it does not restrict LAN admin (ports 80/8088), because
# the chosen policy keeps the WiFi hotspot usable for administration. LAN
# access is gated by the WPA2 WiFi password instead. (/ajax remains
# unauthenticated by the stock firmware — residual risk, documented.)
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

# Phone-home / server-controlled remote-flash channel (outbound).
iptables -C OUTPUT -d $PHONEHOME -j DROP 2>/dev/null || iptables -A OUTPUT -d $PHONEHOME -j DROP

echo "[harden] INPUT chain UZSEC:"
iptables -L UZSEC -n -v
echo "[harden] OUTPUT phone-home:"
iptables -L OUTPUT -n | busybox grep -i "$PHONEHOME"
echo "[harden] done (adbd/USB/WiFi untouched; LAN admin left open per policy)."
