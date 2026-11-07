#!/system/bin/sh
# Start all UZ801 chroot services, detached + logged. Idempotent.
# Called at boot from /data/webui/boot-hook.sh, or run by hand:
#   adb shell /system/bin/sh /data/linux/start-services.sh
#
# enter.sh (run first) sets up the chroot's mounts (tmpfs /dev + devpts + ptmx,
# proc, sys, /dev/smd11, /dev/snd). Each daemon is launched with setsid so it
# survives the launching shell, with stdio redirected so it never holds a pipe.
R=/data/linux/rootfs

# stop any previous instances
busybox pkill -9 -f "sshd -D"   2>/dev/null
busybox pkill -9 -f "server.py" 2>/dev/null
busybox pkill -9 -f "tailscaled" 2>/dev/null
busybox pkill -9 -f "AdGuardHome" 2>/dev/null
sleep 1

# One chroot entry sets up mounts (idempotent) and launches the daemons.
/system/bin/sh /data/linux/enter.sh '
  # SSH (root lifeline)
  (setsid /usr/sbin/sshd -D -e >/var/log/sshd.log 2>&1 &)

  # Cockpit API (HTTPS :443 + serves the SPA)
  (cd /opt/cockpit/api && setsid /bin/sh run.sh >/opt/cockpit/run.log 2>&1 &)

  # Tailscale daemon — only if a TUN dev exists or userspace fallback wanted.
  # Needs ""tailscale up"" (auth) + working WAN to actually connect.
  if [ -x /usr/sbin/tailscaled ]; then
    mkdir -p /var/lib/tailscale /var/run/tailscale
    (setsid /usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state \
        --socket=/var/run/tailscale/tailscaled.sock \
        --tun=userspace-networking >/var/log/tailscaled.log 2>&1 &)
  fi

  # AdGuard Home — only when a config exists (bare first-run wizard is :3000;
  # we do NOT auto-bind :53 to avoid clashing with the stock dnsmasq).
  if [ -x /usr/bin/adguardhome ] && [ -f /opt/cockpit/adguard/AdGuardHome.yaml ]; then
    (cd /opt/cockpit/adguard && setsid /usr/bin/adguardhome -c AdGuardHome.yaml \
        -w /opt/cockpit/adguard >/var/log/adguard.log 2>&1 &)
  fi

  sleep 2
  echo services-started
'
exit 0
