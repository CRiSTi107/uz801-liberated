#!/system/bin/sh
# Boot hook: start the custom UI and apply the firewall at every boot.
# Called (backgrounded) from /system/etc/init.qcom.post_boot.sh.
# Everything is logged; nothing writes to the init stdout pipe.
LOG=/data/webui/boot-hook.log
{
  echo "=== boot-hook $(date 2>/dev/null) ==="
  # (decommissioned) old unauth :8088 shim no longer started — cockpit :443 replaces it
  /system/bin/sh /data/webui/harden.sh
  # Alpine chroot services: SSH + cockpit API (+ tailscale/adguard when configured)
  [ -f /data/linux/start-services.sh ] && /system/bin/sh /data/linux/start-services.sh
  echo "=== boot-hook done ==="
} >> "$LOG" 2>&1
exit 0
