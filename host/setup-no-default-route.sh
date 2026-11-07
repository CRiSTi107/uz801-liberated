#!/bin/sh
# Host-side (Linux PC, NetworkManager): when the UZ801 dongle is plugged in over USB,
# keep reaching it on 192.168.100.0/24 but NEVER make it the default gateway or DNS.
#
#  1. systemd .link: match the dongle by USB ID 05c6:90b6 -> stable iface name "uz801"
#     (the default enx<MAC> name changes if the host-side RNDIS MAC changes)
#  2. NM profile "uz801" bound to that iface: DHCP, never-default, ignore its DNS.
#
# Run once:  sudo sh host/setup-no-default-route.sh   then replug the dongle.
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
  ipv4.method auto ipv4.never-default yes ipv4.ignore-auto-dns yes \
  ipv6.method auto ipv6.never-default yes ipv6.ignore-auto-dns yes

udevadm control --reload
echo "Done. Replug the dongle, then check:  ip route | grep -E 'default|192.168.100'"
