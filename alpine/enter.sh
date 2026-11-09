#!/system/bin/sh
# Enter the Alpine armv7 chroot on the UZ801 (idempotent setup).
# Usage: enter.sh                 -> interactive /bin/sh inside Alpine
#        enter.sh "cmd arg ..."   -> run one command inside Alpine
#
# /dev is built as a private tmpfs (NOT a bind of the host /dev) so we can
# create a Unix98 devpts with a working /dev/ptmx -> real PTYs for sshd.
# Host device nodes the chroot needs (modem /dev/smd11, audio /dev/snd) are
# bound in explicitly. Operating on $R/dev (a path under /data) never touches
# the host's real /dev.
R=/data/linux/rootfs

busybox mount | busybox grep -q "$R/proc " || busybox mount -t proc proc "$R/proc"
busybox mount | busybox grep -q "$R/sys "  || busybox mount -o bind /sys "$R/sys"

if ! busybox mount | busybox grep -q "tmpfs on $R/dev "; then
  busybox umount "$R/dev/pts" 2>/dev/null
  busybox umount "$R/dev"     2>/dev/null   # drop any previous /dev bind
  busybox mount -t tmpfs tmpfs "$R/dev"
  busybox mkdir -p "$R/dev/pts" "$R/dev/shm"
  busybox mknod "$R/dev/null"    c 1 3 && busybox chmod 666 "$R/dev/null"
  busybox mknod "$R/dev/zero"    c 1 5 && busybox chmod 666 "$R/dev/zero"
  busybox mknod "$R/dev/full"    c 1 7 && busybox chmod 666 "$R/dev/full"
  busybox mknod "$R/dev/random"  c 1 8 && busybox chmod 666 "$R/dev/random"
  busybox mknod "$R/dev/urandom" c 1 9 && busybox chmod 666 "$R/dev/urandom"
  busybox mknod "$R/dev/tty"     c 5 0 && busybox chmod 666 "$R/dev/tty"
  # this kernel is single-instance devpts (no newinstance/ptmxmode); the ptmx
  # multiplexor is the classic c 5 2 node, which opens the mounted devpts.
  busybox mount -t devpts -o rw,nosuid,mode=0620,gid=5 devpts "$R/dev/pts"
  busybox mknod "$R/dev/ptmx" c 5 2 && busybox chmod 666 "$R/dev/ptmx"
  busybox mount -t tmpfs tmpfs "$R/dev/shm"
  # modem AT channel (cockpit API) + sound devices (audio bridge)
  [ -e /dev/smd11 ] && { : > "$R/dev/smd11"; busybox mount -o bind /dev/smd11 "$R/dev/smd11"; }
  [ -d /dev/snd ]   && { busybox mkdir -p "$R/dev/snd"; busybox mount -o bind /dev/snd "$R/dev/snd"; }
  # free QMI control channel (cockpit band-lock): qmuxd holds smdcntl0..7,
  # smdcntl8 is unused -> char 244,8. mknod it so the chroot can open it.
  busybox mknod "$R/dev/smdcntl8" c 244 8 2>/dev/null; busybox chmod 660 "$R/dev/smdcntl8" 2>/dev/null
fi

# Host paths the cockpit reads directly (WiFi hostapd.conf, dnsmasq leases, wifi nv)
busybox mount | busybox grep -q "$R/data/misc " || { busybox mkdir -p "$R/data/misc"; busybox mount -o bind /data/misc "$R/data/misc"; }

busybox printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' > "$R/etc/resolv.conf"

PROXY=""
[ -f /data/linux/proxy.env ] && PROXY=$(busybox cat /data/linux/proxy.env)

if [ -n "$1" ]; then
  exec busybox chroot "$R" /usr/bin/env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/root TERM=xterm $PROXY /bin/sh -c "$*"
else
  exec busybox chroot "$R" /usr/bin/env -i \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/root TERM=xterm $PROXY /bin/sh
fi
