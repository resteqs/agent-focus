#!/bin/sh
# Fast, non-blocking agent lifecycle hook for Agent Focus.
# Usage: hook.sh <agent> <start|stop>

AGENT="${1:-agent}"
ACTION="${2:-start}"
PORT="${AGENT_FOCUS_PORT:-7842}"
START_PPID="${PPID:-0}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

PAYLOAD="$(cat 2>/dev/null || true)"

if [ "$AGENT" = "antigravity" ] && [ "$ACTION" = "stop" ]; then
  printf '{"decision":""}\n'
else
  printf '{}\n'
fi

PY_BIN="$(command -v python3 2>/dev/null || echo /opt/homebrew/bin/python3)"
[ -x "$PY_BIN" ] || PY_BIN="/usr/bin/python3"

(
  printf '%s' "$PAYLOAD" | "$PY_BIN" "$SCRIPT_DIR/hook_dispatch.py" "$AGENT" "$ACTION" "$PORT" "$START_PPID" >/dev/null 2>&1
) >/dev/null 2>&1 &

exit 0
