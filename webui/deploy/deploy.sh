#!/usr/bin/env bash
# Deploy the UZ801 custom web UI onto the dongle itself.
#
# It pushes the SPA + busybox-httpd config to /data/webui on the device, starts
# an httpd on :8088 that reverse-proxies /ajax to the stock server on :80, and
# (optionally) installs a boot hook so it auto-starts on every reboot.
#
# Result: open http://192.168.100.1:8088/ from any PC/phone on the dongle's
# WiFi or USB network. No PC-side proxy process required.
#
# Usage:
#   ./deploy.sh [adb-serial]          # push + start + install boot hook
#   ./deploy.sh [adb-serial] nohook   # push + start only (no /system edit)
#   ./deploy.sh [adb-serial] uninstall# remove UI + boot hook from the device
set -euo pipefail

SERIAL="${1:-}"
MODE="${2:-hook}"
ADB=(adb)
[ -n "$SERIAL" ] && ADB=(adb -s "$SERIAL")

HERE="$(cd "$(dirname "$0")/.." && pwd)"   # the webui/ dir
DEST=/data/webui
POST_BOOT=/system/etc/init.qcom.post_boot.sh
HOOK_TAG="# --- UZ801 custom web UI (added) ---"

run() { "${ADB[@]}" shell "$@"; }

if [ "$MODE" = "uninstall" ]; then
  echo "[*] Stopping httpd and removing $DEST ..."
  run 'busybox pkill -f "httpd -p 8088" 2>/dev/null; rm -rf /data/webui' || true
  echo "[*] Removing boot hook from $POST_BOOT ..."
  run "mount -o rw,remount /system 2>/dev/null; \
       busybox sed -i '/UZ801 custom web UI/,+1d' $POST_BOOT 2>/dev/null; \
       mount -o ro,remount /system 2>/dev/null; echo removed" || true
  echo "[*] Done."
  exit 0
fi

echo "[*] Creating $DEST on device ..."
run "rm -rf $DEST && mkdir -p $DEST/css $DEST/js"

echo "[*] Pushing SPA + server config ..."
"${ADB[@]}" push "$HERE/index.html"            "$DEST/index.html"            >/dev/null
"${ADB[@]}" push "$HERE/css/styles.css"        "$DEST/css/styles.css"        >/dev/null
"${ADB[@]}" push "$HERE/js/api.js"             "$DEST/js/api.js"             >/dev/null
"${ADB[@]}" push "$HERE/js/app.js"             "$DEST/js/app.js"             >/dev/null
"${ADB[@]}" push "$HERE/deploy/httpd.conf"     "$DEST/httpd.conf"            >/dev/null
"${ADB[@]}" push "$HERE/deploy/start-webui.sh" "$DEST/start-webui.sh"        >/dev/null
"${ADB[@]}" push "$HERE/deploy/harden.sh"      "$DEST/harden.sh"             >/dev/null
"${ADB[@]}" push "$HERE/deploy/unharden.sh"    "$DEST/unharden.sh"           >/dev/null
"${ADB[@]}" push "$HERE/deploy/boot-hook.sh"   "$DEST/boot-hook.sh"          >/dev/null
run "chmod 755 $DEST/start-webui.sh $DEST/harden.sh $DEST/unharden.sh $DEST/boot-hook.sh"

echo "[*] Starting httpd on :8088 ..."
run "/system/bin/sh $DEST/start-webui.sh"
sleep 1

echo "[*] Verifying on device ..."
run 'busybox wget -q -O - "http://127.0.0.1:8088/" >/dev/null && echo "  static OK" || echo "  static FAIL"'
run 'busybox wget -q -O - --header="Content-Type: application/json" --post-data='"'"'{"funcNo":1001}'"'"' "http://127.0.0.1:8088/ajax"'

if [ "$MODE" = "hook" ]; then
  echo "[*] Installing boot hook into $POST_BOOT ..."
  run "mount -o rw,remount /system; \
       if ! busybox grep -q 'UZ801 custom web UI' $POST_BOOT; then \
         { echo ''; echo '$HOOK_TAG'; echo '[ -f $DEST/boot-hook.sh ] && /system/bin/sh $DEST/boot-hook.sh &'; } >> $POST_BOOT; \
         echo '  hook added'; \
       else echo '  hook already present'; fi; \
       mount -o ro,remount /system"
fi

echo
echo "[*] Done. Open:  http://192.168.100.1:8088/"
