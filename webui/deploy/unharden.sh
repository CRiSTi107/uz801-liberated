#!/system/bin/sh
# Revert harden.sh: remove the firewall chain and the phone-home block.
# NOTE: it deliberately does NOT re-open adb-over-TCP (that is a remote root
# hole). To restore it manually:  setprop persist.adb.tcp.port 7628; stop adbd; start adbd

iptables -D INPUT -j UZSEC 2>/dev/null
iptables -F UZSEC 2>/dev/null
iptables -X UZSEC 2>/dev/null
iptables -D OUTPUT -d 120.77.62.229 -j DROP 2>/dev/null

echo "[unharden] firewall + phone-home block removed. adb-over-TCP left disabled."
