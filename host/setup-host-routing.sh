#!/bin/sh
# Host-side (Linux PC, NetworkManager): when the UZ801 dongle is plugged in over USB,
# keep reaching it on 192.168.100.0/24, and let it be a default gateway ONLY as the
# lowest-priority fallback (used only when no other default route exists).
#
#  1. systemd .link: match the dongle by USB ID 05c6:90b6 -> stable iface name "uz801"
#     (the default enx<MAC> name changes if the host-side RNDIS MAC changes)
#  2. NM profile "uz801" bound to that iface: DHCP, route metric 2000 (WiFi is 600,
#     wired ~100; higher = less preferred), DNS priority 200 (also less preferred).
#
# Run once:  sudo sh host/setup-host-routing.sh   then replug the dongle.
# Want it NEVER to be a gateway instead?  sudo nmcli con mod uz801 ipv4.never-default yes ipv6.never-default yes
set -eu
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

cat > /etc/systemd/network/10-uz801.link <<'EOF'
[Match]
Property=ID_VENDOR_ID=05c6 ID_MODEL_ID=90b6
Type=ether

[Link]
Name=uz801
EOF

# drop any previous profile with this name, then create the persistent one
nmcli -t -f NAME con show | grep -qx uz801 && nmcli con delete uz801
nmcli con add type ethernet con-name uz801 ifname uz801 \
  connection.autoconnect yes connection.autoconnect-priority 10 \
  ipv4.method auto ipv4.route-metric 2000 ipv4.dns-priority 200 \
  ipv6.method auto ipv6.route-metric 2000 ipv6.dns-priority 200

udevadm control --reload
echo "Done. Replug the dongle, then check:  ip route | grep -E 'default|192.168.100'"
