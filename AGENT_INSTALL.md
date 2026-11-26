# Agent Focus: install playbook for AI coding agents

This file is for an AI coding agent (Claude Code, Codex, Antigravity, Cursor, or similar) that a user has asked to install Agent Focus on their Mac. Follow the steps in order. Run each command, check its result, and stop to tell the user if a check fails.

Agent Focus is a small macOS daemon. While a coding agent is working, it redirects distracting Safari tabs to a local wait page at `http://127.0.0.1:7842` with Anki flashcards, a notes scratchpad, and a breathing timer.

## Rules

- Never ask the user to type their AnkiWeb password, or any other password, into this chat. The user signs in to AnkiWeb themselves, in the browser or in their own terminal.
- Do not edit `~/.claude/settings.json`, `~/.codex/hooks.json`, or `~/.gemini/config/hooks.json` by hand. `install.sh` does that.
- Do not use `sudo`. Nothing here needs it.
- If `~/.local/share/agent-focus/config.json` already exists, keep the user's values. Only change the keys the user asks you to change.

## Step 1: Check prerequisites

```bash
sw_vers -productName            # must print macOS
test -d /Applications/Safari.app && echo "Safari OK"
git --version
python3 --version               # needs 3.10 or newer
command -v uv || echo "uv not installed (optional)"
```

Continue if the Mac runs macOS, Safari is present, `git` works, and either `python3` is 3.10 or newer or `uv` is installed. `install.sh` prefers `uv` when it is present.

If `git` is missing, ask the user to run `xcode-select --install` and wait for it to finish. If Python is too old and `uv` is missing, suggest `brew install python@3.12` or `brew install uv`.

## Step 2: Clone and install

Ask the user where to put the checkout. The default is `~/Code/agent-focus`. The LaunchAgent runs from this folder, so it has to stay there.

```bash
mkdir -p ~/Code
git clone https://github.com/resteqs/agent-focus.git ~/Code/agent-focus
cd ~/Code/agent-focus
./install.sh
```

If the folder already exists and is a clone of this repo, run `git pull` in it and then `./install.sh` again. Running the installer twice is safe.

`install.sh` does the following:

- creates a venv at `~/.local/share/agent-focus/venv` with the `anki` package
- copies `config.example.json` to `~/.local/share/agent-focus/config.json` if that file does not exist yet
- links the `agent-focus` command into `~/.local/bin`
- installs and starts the LaunchAgent `com.agent-focus.daemon`
- adds start and stop hooks to Claude Code, Codex, and Antigravity when their config files exist

Check that it worked:

```bash
command -v agent-focus || echo 'Add ~/.local/bin to PATH'
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:7842/api/status   # expect 200
```

If `agent-focus` is not on `PATH`, call it as `~/.local/bin/agent-focus` for the rest of this playbook and tell the user to add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc`.

## Step 3: Ask three preferences and save them

Ask the user these three questions together, in one short message:

1. Which app should come to the front when agent runs finish? Examples: `Orca`, `Cursor`, `Terminal`, `Visual Studio Code`, `iTerm`. This is `return_app`. Use the name exactly as it appears in `/Applications`, without `.app`.
2. Which sites distract you? Show the current list from the config and ask what to add or remove. This is `blocked_domains`. Use bare hostnames such as `reddit.com`. A domain also blocks its subdomains.
3. Do you want the Notes tab to append to an Obsidian note? If yes, ask for the full path to a Markdown file, for example `~/Obsidian/Vault/Agent Scratchpad.md`. This is `obsidian_note_path`. Leave it empty to skip.

Show the current values first:

```bash
python3 -c 'import json, pathlib; c = json.loads((pathlib.Path.home() / ".local/share/agent-focus/config.json").read_text()); print(json.dumps({k: c.get(k) for k in ("return_app", "blocked_domains", "obsidian_note_path")}, indent=2))'
```

Then write only the answers back. Replace the three values in this command with the user's answers:

```bash
python3 - <<'PY'
import json, pathlib
path = pathlib.Path.home() / ".local/share/agent-focus/config.json"
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

## Step 4: Grant Safari Automation permission

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

## Step 5: Sign in to AnkiWeb

Flashcards sync directly with AnkiWeb, or with a self-hosted Anki sync server. Anki Desktop is not needed.

Never ask the user for their password in this chat. Give them one of these two options and let them do it:

- **Browser (easiest).** Open the Flashcards tab for them:

  ```bash
  open "http://127.0.0.1:7842/?tab=anki"
  ```

  Tell the user to enter their AnkiWeb email and password in **Connect AnkiWeb** and click **Sign in**. Cards show up within seconds; images keep downloading in the background. A self-hosted server is set from the terminal (below) or with `anki_sync_endpoint` in `config.json`.

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

`agent-focus status` prints JSON from the daemon. `agent-focus demo 15` fakes a 15-second agent run and opens Safari on the wait page. Ask the user to confirm that:

1. Safari shows the Agent Focus wait page.
2. The Flashcards tab shows their cards, or a "Connect AnkiWeb" prompt if they skipped Step 5.
3. After about 15 seconds the page says the run is done and offers a button to return to their app.

Then tell the user it is installed, and mention these commands:

- `agent-focus open` opens the wait page.
- `./uninstall.sh` in the checkout removes the LaunchAgent, the CLI link, and the hooks.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `curl` to port 7842 fails | `agent-focus restart`, then `agent-focus logs`. |
| Tabs are not redirected | Step 4: check Safari Automation permission. Check the site is in `blocked_domains`. |
| Hooks do not fire | Restart the coding agent so it reloads its hooks file, then run `./install.sh` again. |
| `Sign-in failed` | The user checks their AnkiWeb email and password at ankiweb.net, then tries again. |
| Sync reports a conflict (HTTP 409) | The local cache and AnkiWeb disagree. With the user's OK, replace the local cache with the AnkiWeb copy: `curl -s -X POST -H "Content-Type: application/json" -d '{"on_conflict":"download"}' http://127.0.0.1:7842/api/anki/sync`. Unsynced local reviews are lost. |
