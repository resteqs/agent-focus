#!/usr/bin/env bash
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.agent-focus.daemon.plist"
PY_BIN="$(command -v python3 || echo /opt/homebrew/bin/python3)"

if [ -f "$PLIST_PATH" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
  rm -f "$PLIST_PATH"
fi

rm -f "$HOME/.local/bin/agent-focus"

"$PY_BIN" - <<'PY'
import json
from pathlib import Path

def save_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def strip_group_hooks(path: Path) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    hooks = data.get("hooks", {})
    for ev, entries in list(hooks.items()):
        hooks[ev] = [
            e for e in entries
            if not any("agent-focus" in str(h.get("command", "")) for h in e.get("hooks", []))
        ]
    save_atomic(path, data)

strip_group_hooks(Path.home() / ".claude/settings.json")
strip_group_hooks(Path.home() / ".codex/hooks.json")

agy_path = Path.home() / ".gemini/config/hooks.json"
if agy_path.exists():
    data = json.loads(agy_path.read_text(encoding="utf-8"))
    data.pop("agent-focus", None)
    save_atomic(agy_path, data)
PY

echo "Agent Focus uninstalled. Personal config and scratchpad remain in ~/.local/share/agent-focus."
