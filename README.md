# Agent Focus

A coding agent takes a few minutes per turn, and a few minutes is just long enough to open Reddit and lose twenty. Agent Focus closes that gap on macOS.

It hooks into Claude Code, Codex, and Antigravity. While any agent is working, Safari tabs on the sites you list are redirected to a local page at `http://127.0.0.1:7842`. When the last run finishes, the page tells you and offers a button back to your editor.

The wait page has four tabs:

- **Flashcards.** Review your Anki decks. Ratings go to Anki's real scheduler (SM-2 or FSRS) and sync back to AnkiWeb. `Space` shows the answer, `1` to `4` rate, `S` skips.
- **Notes.** A scratchpad that saves as you type. `⌘↩` appends it to an Obsidian note.
- **Rest.** A slow breathing dial, for when you'd rather look away from a screen.
- **Settings.** Blocked sites, sounds, auto-restore, and Anki sign-in.

If you really need a blocked site (a 2FA email, say), the bypass button unlocks after 30 seconds and opens the site for 2 minutes. The server enforces the wait, so reloading the page doesn't skip it.

No `sudo`, no browser extension, no system proxy. The daemon only listens on `127.0.0.1`.

## Install

```bash
git clone https://github.com/<your-username>/agent-focus.git
cd agent-focus
./install.sh
agent-focus demo
```

`install.sh` creates a LaunchAgent, links the `agent-focus` command into `~/.local/bin`, and adds hooks to Claude Code (`~/.claude/settings.json`), Codex (`~/.codex/hooks.json`), and Antigravity (`~/.gemini/config/hooks.json`) when those files exist. It also creates a small venv with the `anki` package for direct sync. Set `AGENT_FOCUS_NO_VENV=1` to skip that and run on the system Python 3 alone.

## Anki

Agent Focus looks for your cards in three places, in this order:

1. **Its own synced collection (recommended).** Sign in once and Agent Focus syncs with AnkiWeb directly through the official `anki` package. Anki Desktop doesn't need to be open, and no sync dialogs pop up. Reviews are pushed about a minute after your last rating, and again when your agents finish.
2. **Anki Desktop with [AnkiConnect](https://ankiweb.net/shared/info/2055492159).** Ratings go straight into the open collection. Agent Focus never starts an AnkiConnect sync on its own, because that opens a dialog in Anki. Press **Sync** when you want one.
3. **A read-only copy** of Anki Desktop's collection, for when neither is available. Reviews are counted locally and not written back.

### Sign in

From the **Settings** tab, or from a terminal:

```bash
agent-focus anki login                                       # AnkiWeb
agent-focus anki login --endpoint https://sync.example.com/  # self-hosted sync server
agent-focus anki status
agent-focus anki sync
agent-focus anki logout
```

Your password is sent to the sync server once, in exchange for a sync key. Only the key, the endpoint, and your username are saved, in `~/.local/share/anki-headless/agent-focus/auth.json` with `0600` permissions. The synced collection lives in the same folder, separate from Anki Desktop's.

A self-hosted server is anything that speaks the Anki sync protocol, including the one that ships with Anki: `python -m anki.syncserver`.

## Commands

```bash
agent-focus status         # active agent sessions and config
agent-focus demo [secs]    # fake a 35-second agent run and open Safari
agent-focus open           # open the wait page
agent-focus on | off       # block sites even with no agent running
agent-focus pause [mins]   # stop blocking for a while (default 10)
agent-focus resume
agent-focus clear          # forget stuck sessions
agent-focus anki ...       # see above
agent-focus restart        # restart the LaunchAgent
agent-focus logs
```

## Configuration

`~/.local/share/agent-focus/config.json` is created from `config.example.json` on first install and is never overwritten. Most of it can be edited in **Settings**.

| Key | Default | Meaning |
| --- | --- | --- |
| `blocked_domains` | see example | Hostnames to redirect. `reddit.com` also catches `old.reddit.com`. List `mail.proton.me` to block webmail but not the rest of `proton.me`. |
| `bypass_cooldown_seconds` | `30` | Wait before the bypass button unlocks. |
| `default_bypass_seconds` | `120` | How long a bypassed site stays open. |
| `auto_restore` | `false` | Reopen the blocked site when the last run ends, unless you're mid-card or typing. |
| `return_app` | `Orca` | App the **Return to** button brings forward. |
| `obsidian_note_path` | empty | Markdown file the Notes tab appends to. |
| `anki_sync_endpoint` | empty | Self-hosted sync server. Empty means AnkiWeb. |
| `anki_profile_dir` | `~/Library/Application Support/Anki2/User 1` | Anki Desktop profile, used only for the read-only copy. |

## Development

The tests use the standard library's `unittest`. They put all data in a temporary folder, start a throwaway `anki.syncserver` for the sync tests, and never touch your real config or collections.

```bash
~/.local/share/agent-focus/venv/bin/python3 -m unittest discover -s tests -v
```

Without the `anki` package, the sync tests are skipped and the rest still run.

## Uninstall

```bash
./uninstall.sh
```

This removes the LaunchAgent, the CLI link, and the agent hooks. Your config, notes, and synced collection stay in `~/.local/share`.

## License

MIT
