# Agent Focus: install playbook for AI coding agents

This file is for an AI coding agent (Claude Code, Codex, Antigravity, Cursor, or similar) that a user has asked to install Agent Focus. Follow the steps in order. Run each command, check its result, and stop to tell the user if a check fails.

Agent Focus is a small daemon. While a coding agent is working, it redirects distracting browser tabs to a local wait page at `http://127.0.0.1:7842` with Anki flashcards, a notes scratchpad, and a breathing timer. It runs on macOS (tested daily), Windows, and Linux (both newer; tell the user that feedback is welcome).

## Rules

- Never ask the user to type their AnkiWeb password, or any other password, into this chat. The user signs in to AnkiWeb themselves, in the browser or in their own terminal.
- Do not edit `~/.claude/settings.json`, `~/.codex/hooks.json`, or `~/.gemini/config/hooks.json` by hand. The installer does that.
- Do not use `sudo` or an administrator shell. Nothing here needs it.
- If `config.json` in the data folder already exists, keep the user's values. Only change the keys the user asks you to change.

In this playbook, **DATA** means the data folder: `~/.local/share/agent-focus` on macOS and Linux, `%LOCALAPPDATA%\agent-focus` on Windows.

## Step 1: Detect the OS and browser, check prerequisites

Find the OS first. On Windows you are usually in PowerShell; elsewhere in a POSIX shell.

```bash
uname -s 2>/dev/null || echo Windows    # Darwin = macOS, Linux = Linux
```

Then ask the user one question: **which browser do you use for the sites you want blocked?** Safari, Chrome, Arc, Brave, Edge, Vivaldi, or Firefox. Remember the answer:

- **Safari on macOS:** no extension. Do Step 4a.
- **Any other browser, on any OS:** the user loads the extension in `extension/`. Do Step 4b.
- Several browsers: do 4a for Safari and 4b for each of the others.

Check prerequisites for the OS.

**macOS:**

```bash
sw_vers -productName
git --version
python3 --version               # needs 3.10 or newer
command -v uv || echo "uv not installed (optional)"
```

If `git` is missing, ask the user to run `xcode-select --install`. If Python is too old and `uv` is missing, suggest `brew install python@3.12` or `brew install uv`.

**Linux:**

```bash
git --version
python3 --version               # needs 3.10 or newer, with the venv module
systemctl --user --version >/dev/null && echo "systemd user services OK"
command -v uv || echo "uv not installed (optional)"
```

If `python3 -m venv` fails, the user installs their distro's venv package (for example `sudo apt install python3-venv`; they run it themselves). Without systemd user services the installer cannot start the daemon; tell the user and stop.

**Windows (PowerShell):**

```powershell
git --version
py -3 --version                 # or: python --version. Needs 3.10 or newer.
Get-Command uv -ErrorAction SilentlyContinue
```

If Python is missing, suggest `winget install Python.Python.3.12`, then a new terminal.

## Step 2: Clone and install

Ask the user where to put the checkout. The default is `~/Code/agent-focus`. The background service runs from this folder, so it has to stay there. If the folder already exists and is a clone of this repo, run `git pull` in it and then the installer again. Running an installer twice is safe.

**macOS and Linux:**

```bash
mkdir -p ~/Code
git clone https://github.com/resteqs/agent-focus.git ~/Code/agent-focus
cd ~/Code/agent-focus
./install.sh
```

**Windows (PowerShell):**

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\Code" | Out-Null
git clone https://github.com/resteqs/agent-focus.git "$HOME\Code\agent-focus"
cd "$HOME\Code\agent-focus"
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer:

- creates a venv in DATA with the `anki` package
- copies `config.example.json` to DATA/`config.json` if that file does not exist yet
- adds the `agent-focus` command: a link in `~/.local/bin` (macOS, Linux), or `agent-focus.cmd` in DATA, added to the user `PATH` (Windows)
- starts the daemon as a LaunchAgent (macOS), a systemd user service (Linux), or the logon task `AgentFocusDaemon` (Windows)
- adds start and stop hooks to Claude Code, Codex, and Antigravity when their config files exist

Check that it worked:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7842/api/status   # expect 200
```

On Windows, use `(Invoke-WebRequest http://127.0.0.1:7842/api/status -UseBasicParsing).StatusCode` and expect `200`.

If `agent-focus` is not found: on macOS and Linux call it as `~/.local/bin/agent-focus` and tell the user to add `export PATH="$HOME/.local/bin:$PATH"` to their shell profile. On Windows, open a new terminal so the updated `PATH` applies, or call `"$env:LOCALAPPDATA\agent-focus\agent-focus.cmd"`.

## Step 3: Ask three preferences and save them

Ask the user these three questions together, in one short message:

1. Which app should come to the front when agent runs finish? Examples: `Orca`, `Cursor`, `Terminal`, `Visual Studio Code`, `iTerm`. This is `return_app`. On macOS, use the name exactly as it appears in `/Applications`, without `.app`. On Windows and Linux, use a word from the app's window title, such as `Cursor` or `Code`.
2. Which sites distract you? Show the current list from the config and ask what to add or remove. This is `blocked_domains`. Use bare hostnames such as `reddit.com`. A domain also blocks its subdomains.
3. Do you want the Notes tab to append to an Obsidian note? If yes, ask for the full path to a Markdown file, for example `~/Obsidian/Vault/Agent Scratchpad.md`. This is `obsidian_note_path`. Leave it empty to skip.

On Linux, bringing an app forward needs `wmctrl` or `xdotool`. If neither is installed, mention that the **Return to** button will not work until the user installs one.

Show the current values first (on Windows, use `py` instead of `python3`; the code below finds the right folder on every OS):

```bash
python3 -c 'import json, os, pathlib; d = pathlib.Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else pathlib.Path.home() / ".local/share"; c = json.loads((d / "agent-focus/config.json").read_text()); print(json.dumps({k: c.get(k) for k in ("return_app", "blocked_domains", "obsidian_note_path")}, indent=2))'
```

Then write only the answers back. Replace the three values in this script with the user's answers and run it. On Windows, save the Python lines between `<<'PY'` and `PY` to a temporary `.py` file, run it with `py`, then run `agent-focus restart`.

```bash
python3 - <<'PY'
import json, os, pathlib
data = pathlib.Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else pathlib.Path.home() / ".local/share"
path = data / "agent-focus/config.json"
cfg = json.loads(path.read_text())

cfg["return_app"] = "Cursor"                       # answer 1
add = ["reddit.com"]                               # answer 2, sites to add
remove = []                                        # answer 2, sites to remove
cfg["blocked_domains"] = [d for d in dict.fromkeys(cfg.get("blocked_domains", []) + add) if d not in remove]
cfg["obsidian_note_path"] = ""                     # answer 3, or "" to skip

path.write_text(json.dumps(cfg, indent=2) + "\n")
print("Saved", path)
PY
agent-focus restart
```

The user can change all of this later in the Settings tab.

## Step 4a: Safari on macOS: grant Automation permission

Agent Focus reads and redirects Safari tabs with AppleScript. macOS asks once for permission. Trigger the prompt on purpose now, so it does not interrupt the user later.

Tell the user first: "A macOS dialog will ask whether to allow controlling Safari. Please click Allow."

```bash
open -a Safari https://example.com
agent-focus on        # block sites now, even with no agent running
sleep 5
agent-focus off
```

While blocking is on, the daemon polls Safari, which shows the macOS prompt. Ask the user whether they saw it and clicked Allow.

Check the log for permission errors:

```bash
agent-focus logs | grep -i "applescript error" | tail -n 5
```

If that prints errors that mention "not authorized" or `-1743`, the permission was denied. Ask the user to open **System Settings > Privacy & Security > Automation**, find the Python or `osascript` entry, and turn on **Safari**. Then run `agent-focus restart` and repeat this step.

## Step 4b: Chrome, Arc, Brave, Edge, Vivaldi, or Firefox: load the extension

The extension lives in the `extension` folder of the checkout. A browser only lets the user load it by hand, so walk them through it. Print the full path first, so they can find it in the file picker:

```bash
echo "$HOME/Code/agent-focus/extension"        # or the folder chosen in Step 2
```

**Chromium browsers (Chrome, Arc, Brave, Edge, Vivaldi).** Tell the user:

1. Open the extensions page: `chrome://extensions` (Arc: `arc://extensions`, Brave: `brave://extensions`, Edge: `edge://extensions`, Vivaldi: `vivaldi://extensions`).
2. Turn on **Developer mode** (top right; in Edge, the left sidebar).
3. Click **Load unpacked** and choose the `extension` folder.
4. Agent Focus appears in the list with an amber dot icon.

**Firefox.** Tell the user:

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on** and choose `extension/manifest.json`.
3. Open `about:addons`, click Agent Focus, open **Permissions**, and allow access to `127.0.0.1`. Without this, Firefox blocks the extension from reaching the daemon.
4. Temporary add-ons are removed when Firefox quits. Tell the user to repeat these steps after a restart, or to use Firefox Developer Edition with `xpinstall.signatures.required` set to `false` in `about:config` for a permanent install.

Check that it works:

```bash
agent-focus on        # block sites now, even with no agent running
```

Ask the user to open a blocked site, such as `https://reddit.com`, in that browser. It should land on the Agent Focus wait page within a second. Then run `agent-focus off`. If the tab is not redirected, ask the user to check that the extension is enabled, and in Firefox that the `127.0.0.1` permission is allowed.

## Step 5: Sign in to AnkiWeb

Flashcards sync directly with AnkiWeb, or with a self-hosted Anki sync server. Anki Desktop is not needed.

Never ask the user for their password in this chat. Give them one of these two options and let them do it:

- **Browser (easiest).** Open the Flashcards tab for them:

  ```bash
  open "http://127.0.0.1:7842/?tab=anki"          # macOS
  xdg-open "http://127.0.0.1:7842/?tab=anki"      # Linux
  start "http://127.0.0.1:7842/?tab=anki"         # Windows
  ```

  Tell the user to enter their AnkiWeb email and password in the **Sign in to Anki** card and click **Sign in**. Cards show up within seconds; images keep downloading in the background. A self-hosted server is set from the terminal (below) or with `anki_sync_endpoint` in `config.json`.

- **Terminal.** Tell the user to run this in their own terminal window, not through you, because it prompts for a password:

  ```bash
  agent-focus anki login
  agent-focus anki login --endpoint https://sync.example.com/   # self-hosted server
  ```

The password is sent once to get a sync key and is never saved. If the user has no AnkiWeb account and does not want one, skip this step. Everything else still works.

After the user says they are done, check:

```bash
agent-focus anki status
```

Expect `"signed_in": true` and `"mode": "headless"`. If it shows `"mode": "signed_out"`, the sign-in did not finish. If it shows `"mode": "unavailable"`, the `anki` package is missing; run `./install.sh` again.

## Step 6: Verify end to end

```bash
agent-focus status
agent-focus demo 15
```

`agent-focus status` prints JSON from the daemon. `agent-focus demo 15` fakes a 15-second agent run and opens the wait page (in Safari on macOS, in the default browser elsewhere). Ask the user to confirm that:

1. The browser shows the Agent Focus wait page.
2. The Flashcards tab shows their cards, or a "Sign in to Anki" card if they skipped Step 5.
3. After about 15 seconds the page says the run is done and offers a button to return to their app.

Then tell the user it is installed, and mention these commands:

- `agent-focus open` opens the wait page.
- `./uninstall.sh` (macOS, Linux) or `powershell -ExecutionPolicy Bypass -File .\uninstall.ps1` (Windows) in the checkout removes the service, the CLI command, and the hooks.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `curl` to port 7842 fails | `agent-focus restart`, then `agent-focus logs`. |
| Safari tabs are not redirected | Step 4a: check Safari Automation permission. Check the site is in `blocked_domains`. |
| Other browsers' tabs are not redirected | Step 4b: check the extension is loaded and enabled. In Firefox, allow the `127.0.0.1` permission and reload the add-on after a restart. |
| Windows: daemon not running | `agent-focus restart`, then `agent-focus logs`. In Task Scheduler, check the `AgentFocusDaemon` task's last result. |
| Linux: daemon not running | `systemctl --user status agent-focus` and `journalctl --user -u agent-focus`. |
| **Return to** does nothing on Linux | Install `wmctrl` or `xdotool`. |
| Hooks do not fire | Restart the coding agent so it reloads its hooks file, then run `./install.sh` again. |
| `Sign-in failed` | The user checks their AnkiWeb email and password at ankiweb.net, then tries again. |
| Sync reports a conflict (HTTP 409) | The local cache and AnkiWeb disagree. With the user's OK, replace the local cache with the AnkiWeb copy: `curl -s -X POST -H "Content-Type: application/json" -d '{"on_conflict":"download"}' http://127.0.0.1:7842/api/anki/sync`. Unsynced local reviews are lost. |
