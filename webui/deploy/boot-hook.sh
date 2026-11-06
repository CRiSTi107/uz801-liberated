#!/system/bin/sh
# Boot hook: start the custom UI and apply the firewall at every boot.
# Called (backgrounded) from /system/etc/init.qcom.post_boot.sh.
# Everything is logged; nothing writes to the init stdout pipe.
LOG=/data/webui/boot-hook.log
{
  echo "=== boot-hook $(date 2>/dev/null) ==="
  /system/bin/sh /data/webui/start-webui.sh
  /system/bin/sh /data/webui/harden.sh
  echo "=== boot-hook done ==="
} >> "$LOG" 2>&1
exit 0
