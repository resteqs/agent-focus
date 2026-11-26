#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.agent-focus.daemon.plist"
UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/agent-focus.service"
PY_BIN="$(command -v python3 || echo /opt/homebrew/bin/python3)"

if [ -f "$PLIST_PATH" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
fi

if [ -f "$UNIT_PATH" ]; then
  systemctl --user disable --now agent-focus 2>/dev/null || true
  rm -f "$UNIT_PATH"
  systemctl --user daemon-reload 2>/dev/null || true
fi

rm -f "$HOME/.local/bin/agent-focus"

"$PY_BIN" "$REPO_DIR/hooks/wire_hooks.py" uninstall

echo "Agent Focus uninstalled. Personal config and scratchpad remain in ~/.local/share/agent-focus."
