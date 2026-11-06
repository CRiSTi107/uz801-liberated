#!/system/bin/sh
# Start the UZ801 custom web UI (busybox httpd) on port 8088.
# busybox httpd daemonizes itself (double-fork) when NOT given -f, so it keeps
# running after this script and the launching adb shell exit.
# Idempotent: kills any previous instance first. Safe to run repeatedly.
# Invoked at boot from /system/etc/init.qcom.post_boot.sh and can be run by hand.

WEBROOT=/data/webui
PORT=8088

busybox pkill -f "httpd -p ${PORT}" 2>/dev/null
sleep 1
# Redirect stdio so the daemon does not hold the launching shell's (adb) pipe
# open — otherwise `adb shell` hangs and the server gets torn down.
busybox httpd -p ${PORT} -h "${WEBROOT}" -c "${WEBROOT}/httpd.conf" >/data/webui/httpd.log 2>&1
exit 0
