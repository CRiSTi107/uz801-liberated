# host/ — PC-side helpers

## Dongle routing priority
`sudo sh host/setup-host-routing.sh`, then replug the dongle.
Pins the dongle's interface name to `uz801` (by USB ID `05c6:90b6`) and gives it route
metric **2000**, so it's the lowest-priority default gateway: traffic uses WiFi/wired
first and only falls back to the dongle when nothing else is up. `192.168.100.0/24`
(SSH / cockpit) always goes to the dongle.

Check: `ip route | grep -E 'default|192.168.100'`

## Quick manual fixes (no setup, until next replug)
```sh
# drop the dongle's default route right now
sudo ip route del default via 192.168.100.1 dev enx1e810f72d768   # or: dev uz801 after setup

# never use the dongle as a gateway at all (after setup)
sudo nmcli con mod uz801 ipv4.never-default yes ipv6.never-default yes && sudo nmcli con up uz801
```
The `enx…` name is derived from the host-side MAC and may change; `ip -br link` shows the current one.
