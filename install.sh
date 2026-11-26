#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
DATA_DIR="${AGENT_FOCUS_DATA_DIR:-$HOME/.local/share/agent-focus}"
BIN_DIR="$HOME/.local/bin"
PLIST_PATH="$HOME/Library/LaunchAgents/com.agent-focus.daemon.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.antoni.agent-focus.plist"
PY_BIN="$(command -v python3 || echo /opt/homebrew/bin/python3)"
VENV_DIR="$DATA_DIR/venv"

mkdir -p "$DATA_DIR" "$BIN_DIR" "$HOME/Library/LaunchAgents"
chmod +x "$REPO_DIR/server.py" "$REPO_DIR/hooks/hook.sh" "$REPO_DIR/hooks/hook_dispatch.py" "$REPO_DIR/bin/agent-focus"

# Set up lightweight venv with official headless `anki` package unless disabled
if [ "${AGENT_FOCUS_NO_VENV:-0}" != "1" ]; then
  if command -v uv >/dev/null 2>&1; then
    (uv venv --python 3.12 "$VENV_DIR" 2>/dev/null || uv venv "$VENV_DIR") && \
      uv pip install --python "$VENV_DIR/bin/python3" -r "$REPO_DIR/requirements.txt" || true
  elif [ ! -x "$VENV_DIR/bin/python3" ]; then
    "$PY_BIN" -m venv "$VENV_DIR" 2>/dev/null && \
      "$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt" || true
  fi
  if [ -x "$VENV_DIR/bin/python3" ]; then
    PY_BIN="$VENV_DIR/bin/python3"
  fi
fi

# Seed default config on first install (never overwrites existing config)
if [ ! -f "$DATA_DIR/config.json" ]; then
  cp "$REPO_DIR/config.example.json" "$DATA_DIR/config.json"
  echo "Created config at $DATA_DIR/config.json"
fi

# Symlink CLI into ~/.local/bin
ln -sf "$REPO_DIR/bin/agent-focus" "$BIN_DIR/agent-focus"

# Unload legacy plist if present
if [ -f "$LEGACY_PLIST" ]; then
  launchctl bootout "gui/$(id -u)" "$LEGACY_PLIST" 2>/dev/null || true
  rm -f "$LEGACY_PLIST"
fi

# Write LaunchAgent pointing to this checkout
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agent-focus.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PY_BIN}</string>
        <string>${REPO_DIR}/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${DATA_DIR}/server.log</string>
    <key>StandardErrorPath</key>
    <string>${DATA_DIR}/server.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>AGENT_FOCUS_DATA_DIR</key>
        <string>${DATA_DIR}</string>
    </dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

# Wire Claude Code, Codex, and Antigravity hooks if those configs exist
"$PY_BIN" - "$REPO_DIR/hooks/hook.sh" <<'PY'
import json, shutil, sys
from pathlib import Path

HOOK = sys.argv[1]

def save_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def replace_or_add_group(hooks_map: dict, event: str, cmd: str, extra: dict | None = None) -> None:
    entries = hooks_map.setdefault(event, [])
    # Update any previous agent-focus hook path in place
    for entry in entries:
        for h in entry.get("hooks", []):
            c = str(h.get("command", ""))
            if "agent-focus" in c and c.endswith(cmd.split("hook.sh", 1)[1]):
                h["command"] = cmd
                return
    handler = {"type": "command", "command": cmd, "timeout": 5}
    if extra:
        handler.update(extra)
    entries.append({"hooks": [handler]})

# 1. Claude Code
claude_path = Path.home() / ".claude/settings.json"
if claude_path.exists():
    data = json.loads(claude_path.read_text(encoding="utf-8"))
    hooks = data.setdefault("hooks", {})
    replace_or_add_group(hooks, "UserPromptSubmit", f"{HOOK} claude start", {"async": True})
    replace_or_add_group(hooks, "Stop", f"{HOOK} claude stop")
    replace_or_add_group(hooks, "StopFailure", f"{HOOK} claude stop")
    replace_or_add_group(hooks, "SessionEnd", f"{HOOK} claude stop")
    save_atomic(claude_path, data)
    print(f"Wired Claude Code hooks: {claude_path}")

# 2. Codex
codex_path = Path.home() / ".codex/hooks.json"
if codex_path.exists():
    data = json.loads(codex_path.read_text(encoding="utf-8"))
    hooks = data.setdefault("hooks", {})
    replace_or_add_group(hooks, "UserPromptSubmit", f"{HOOK} codex start")
    replace_or_add_group(hooks, "Stop", f"{HOOK} codex stop")
    replace_or_add_group(hooks, "Interrupt", f"{HOOK} codex stop")
    save_atomic(codex_path, data)
    print(f"Wired Codex hooks: {codex_path}")

# 3. Antigravity
agy_path = Path.home() / ".gemini/config/hooks.json"
if agy_path.exists():
    data = json.loads(agy_path.read_text(encoding="utf-8"))
    data["agent-focus"] = {
        "PreInvocation": [{"type": "command", "command": f"{HOOK} antigravity start", "timeout": 5}],
        "Stop": [{"type": "command", "command": f"{HOOK} antigravity stop", "timeout": 5}],
    }
    save_atomic(agy_path, data)
    print(f"Wired Antigravity hooks: {agy_path}")
PY

echo ""
echo "Agent Focus installed and running at http://127.0.0.1:7842"
echo "Try: agent-focus demo"
