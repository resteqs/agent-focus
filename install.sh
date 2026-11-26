#!/usr/bin/env bash
set -euo pipefail

# macOS (LaunchAgent) and Linux (systemd user service). Windows uses install.ps1.
REPO_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
OS="$(uname -s)"
DATA_DIR="${AGENT_FOCUS_DATA_DIR:-$HOME/.local/share/agent-focus}"
BIN_DIR="$HOME/.local/bin"
PLIST_PATH="$HOME/Library/LaunchAgents/com.agent-focus.daemon.plist"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_PATH="$UNIT_DIR/agent-focus.service"
PY_BIN="$(command -v python3 || echo /opt/homebrew/bin/python3)"
VENV_DIR="$DATA_DIR/venv"

case "$OS" in
  Darwin|Linux) ;;
  *) echo "Unsupported OS: $OS. On Windows, run install.ps1 in PowerShell." >&2; exit 1 ;;
esac

mkdir -p "$DATA_DIR" "$BIN_DIR"
chmod +x "$REPO_DIR/server.py" "$REPO_DIR/hooks/hook.sh" "$REPO_DIR/hooks/hook_dispatch.py" "$REPO_DIR/hooks/wire_hooks.py" "$REPO_DIR/bin/agent-focus"

# Optional venv with the `anki` package for direct AnkiWeb sync. AGENT_FOCUS_NO_VENV=1 skips it.
ANKI_WARNING="Could not install the anki package. Agent Focus will run without direct AnkiWeb sync."
if [ "${AGENT_FOCUS_NO_VENV:-0}" != "1" ]; then
  if command -v uv >/dev/null 2>&1; then
    { uv venv --python 3.12 "$VENV_DIR" 2>/dev/null || uv venv "$VENV_DIR"; } && \
      uv pip install --python "$VENV_DIR/bin/python3" -r "$REPO_DIR/requirements.txt" || echo "$ANKI_WARNING"
  elif [ ! -x "$VENV_DIR/bin/python3" ]; then
    "$PY_BIN" -m venv "$VENV_DIR" 2>/dev/null && \
      "$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt" || echo "$ANKI_WARNING"
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

if [ "$OS" = "Darwin" ]; then
mkdir -p "$HOME/Library/LaunchAgents"

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

else
# Linux: a systemd user service, restarted if it crashes, logging next to the config.
mkdir -p "$UNIT_DIR"
cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=Agent Focus daemon
After=default.target

[Service]
ExecStart="${PY_BIN}" "${REPO_DIR}/server.py"
Environment=AGENT_FOCUS_DATA_DIR=${DATA_DIR}
Restart=always
RestartSec=2
StandardOutput=append:${DATA_DIR}/server.log
StandardError=append:${DATA_DIR}/server.log

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now agent-focus
systemctl --user restart agent-focus
fi

# Wire Claude Code, Codex, and Antigravity hooks if those configs exist
"$PY_BIN" "$REPO_DIR/hooks/wire_hooks.py" install "$REPO_DIR/hooks/hook.sh"

echo ""
echo "Agent Focus installed and running at http://127.0.0.1:7842"
echo "Try: agent-focus demo"
echo "Sign in to AnkiWeb for flashcards: agent-focus anki login"
echo "Using Chrome, Arc, Brave, Edge, or Firefox? Load the extension in $REPO_DIR/extension (README: Browser support)."
