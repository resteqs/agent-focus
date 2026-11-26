# Agent Focus

A coding agent takes a few minutes per turn, and a few minutes is just long enough to open Reddit and lose twenty. Agent Focus closes that gap on macOS.

It hooks into Claude Code, Codex, and Antigravity. While any agent is working, Safari tabs on the sites you list are redirected to a local page at `http://127.0.0.1:7842`. When the last run finishes, the page tells you and offers a button back to your editor.

## Install with an AI agent

Paste this into Claude Code, Codex, Antigravity, Cursor, or any coding agent that can run shell commands:

```text
Read https://raw.githubusercontent.com/resteqs/agent-focus/main/AGENT_INSTALL.md and set up Agent Focus on my Mac.
```

The agent follows [AGENT_INSTALL.md](AGENT_INSTALL.md). It checks prerequisites, runs the installer, asks you three questions, walks you through the Safari permission prompt, and verifies the install. It never asks for your AnkiWeb password. You sign in yourself.

## Manual install

```bash
git clone https://github.com/resteqs/agent-focus.git && cd agent-focus && ./install.sh
agent-focus demo
```

You need macOS, Safari, `git`, and Python 3.10 or newer (or `uv`).

`install.sh` creates a venv with the `anki` package, a LaunchAgent that keeps the daemon running, and an `agent-focus` command in `~/.local/bin`. It adds start and stop hooks to Claude Code (`~/.claude/settings.json`), Codex (`~/.codex/hooks.json`), and Antigravity (`~/.gemini/config/hooks.json`) when those files exist. Keep the checkout where you cloned it, because the LaunchAgent runs from there.

The first time the daemon reads Safari's tabs, macOS asks whether it may control Safari. Click **Allow**.

## How it works

- **Daemon.** `server.py` runs as the LaunchAgent `com.agent-focus.daemon`. It uses only the standard library plus the `anki` package, and listens on `127.0.0.1:7842`.
- **Agent hooks.** Each agent calls `hooks/hook.sh start` when a turn begins and `stop` when it ends. The daemon counts active sessions and drops any that have been silent for 25 minutes, or whose process has exited.
- **Safari watcher.** While at least one session is active, a thread polls the front tab of every Safari window over AppleScript. A tab on a blocked domain is redirected to the wait page, which remembers the original URL.
- **Wait page.** `static/index.html` is one page with four tabs:
  - **Flashcards.** Your Anki decks, scheduled by Anki's real scheduler (SM-2 or FSRS). `Space` shows the answer, `1` to `4` rate, `S` skips.
  - **Notes.** A scratchpad that saves as you type. `⌘↩` appends it to an Obsidian note.
  - **Rest.** A slow breathing dial, for when you'd rather look away from a screen.
  - **Settings.** Blocked sites, sounds, auto-restore, and Anki sign-in.

If you really need a blocked site (a 2FA email, say), the bypass button unlocks after 30 seconds and opens the site for 2 minutes. The server enforces the wait, so reloading the page doesn't skip it.

No `sudo`, no browser extension, no system proxy.

## Anki sync

Agent Focus syncs with AnkiWeb directly, through the official `anki` Python package. Anki Desktop does not need to be installed or open.

- **Sign in once.** In **Settings > Anki sync**, or with `agent-focus anki login`.
- **Silent sync.** Reviews are pushed about a minute after your last rating, and again when your agents finish. No dialogs appear.
- **Separate local cache.** The synced collection lives in `~/.local/share/anki-headless/agent-focus/`, apart from any Anki Desktop profile.
- **Password never saved.** Your password is sent to the sync server once, in exchange for a sync key. Only the key, the endpoint, and your username are kept, in `auth.json` with `0600` permissions.

```bash
agent-focus anki login                                       # AnkiWeb
agent-focus anki login --endpoint https://sync.example.com/  # self-hosted sync server
agent-focus anki status
agent-focus anki sync
agent-focus anki logout                                      # deletes the sync key
```

### Self-hosted sync server

Any server that speaks the Anki sync protocol works, including the one that ships with the `anki` package:

```bash
SYNC_USER1=me:secret SYNC_PORT=8080 python -m anki.syncserver
agent-focus anki login --endpoint http://127.0.0.1:8080/
```

Point Anki on your phone at the same server and your reviews flow both ways.

## CLI commands

```bash
agent-focus status         # active agent sessions and config
agent-focus demo [secs]    # fake an agent run (default 35s) and open Safari
agent-focus open           # open the wait page
agent-focus on | off       # block sites even with no agent running
agent-focus pause [mins]   # stop blocking for a while (default 10)
agent-focus resume
agent-focus clear          # forget stuck sessions
agent-focus anki ...       # see Anki sync
agent-focus restart        # restart the LaunchAgent
agent-focus logs
```

## Configuration

`~/.local/share/agent-focus/config.json` is created from `config.example.json` on first install and is never overwritten. Most of it can be edited in **Settings**. Run `agent-focus restart` after editing the file by hand.

| Key | Default | Meaning |
| --- | --- | --- |
| `blocked_domains` | see example | Hostnames to redirect. `reddit.com` also catches `old.reddit.com`. List `mail.proton.me` to block webmail but not the rest of `proton.me`. |
| `bypass_cooldown_seconds` | `30` | Wait before the bypass button unlocks. |
| `default_bypass_seconds` | `120` | How long a bypassed site stays open. |
| `auto_restore` | `false` | Reopen the blocked site when the last run ends, unless you're mid-card or typing. |
| `return_app` | `Orca` | App the **Return to** button brings forward. |
| `completion_chime` | `true` | Play a short sound when the last run ends. |
| `obsidian_note_path` | empty | Markdown file the Notes tab appends to. |
| `anki_sync_endpoint` | empty | Self-hosted sync server. Empty means AnkiWeb. |

## Running tests

The tests use the standard library's `unittest`. They put all data in a temporary folder, start a throwaway `anki.syncserver` for the sync tests, and never touch your real config or collections.

```bash
~/.local/share/agent-focus/venv/bin/python3 -m unittest discover -s tests -v
```

Without the `anki` package, the sync tests are skipped and the rest still run.

## Security and privacy

- The daemon binds to `127.0.0.1` only. Nothing is reachable from your network.
- Every request must carry a `Host` of `127.0.0.1` or `localhost`. Requests with a foreign `Origin` are rejected, so other websites can't drive the API (CSRF) or reach it through DNS rebinding.
- The Anki sync key is stored with `0600` permissions. Your password is never written to disk.
- No analytics and no outside requests, apart from Anki sync and the fonts and KaTeX files the wait page loads from public CDNs.

## Uninstall

```bash
./uninstall.sh
```

This removes the LaunchAgent, the CLI link, and the agent hooks. Your config, notes, and synced collection stay in `~/.local/share/agent-focus` and `~/.local/share/anki-headless`. Delete those folders to remove everything.

## License

MIT
