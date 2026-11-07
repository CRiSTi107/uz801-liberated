#!/bin/sh
# run.sh - launcher for cockpit-api.
#
# On the device (Alpine chroot): copy config.example.json to config.json, edit
# it, make sure `apk add python3 openssl` has been done, then run this.
# Launched from boot-hook.sh in production.
#
# Environment overrides:
#   COCKPIT_CONFIG   path to config JSON      (default: ./config.json)
#   COCKPIT_MOCK     set to 1 for --mock      (no hardware; PC development)
#   COCKPIT_ARGS     extra args passed through (e.g. "--no-tls --port 8443")
#
# Examples:
#   ./run.sh                              # device, HTTPS on :443
#   COCKPIT_MOCK=1 COCKPIT_ARGS="--no-tls --port 8443" ./run.sh   # PC dev
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

CONFIG="${COCKPIT_CONFIG:-$DIR/config.json}"
PY="${PYTHON:-python3}"

ARGS="--config $CONFIG"
if [ "${COCKPIT_MOCK:-0}" = "1" ]; then
  ARGS="$ARGS --mock"
fi
ARGS="$ARGS ${COCKPIT_ARGS:-}"

if [ ! -f "$CONFIG" ] && [ "${COCKPIT_MOCK:-0}" != "1" ]; then
  echo "run.sh: $CONFIG not found. Copy config.example.json to config.json and edit it." >&2
  echo "        (or run with COCKPIT_MOCK=1 for offline development.)" >&2
fi

echo "run.sh: exec $PY server.py $ARGS"
exec "$PY" server.py $ARGS
