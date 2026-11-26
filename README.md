# Agent Focus

Stop drifting into Reddit, Hacker News, LinkedIn, news, or email while waiting for a coding agent to finish a turn.

Agent Focus hooks into **Claude Code**, **Codex**, and **Antigravity** on macOS. While any agent is actively running, Safari tabs pointing at your distracting domains redirect to a calm local wait-state page (`http://127.0.0.1:7842`) with:

- **Two-way Anki flashcards** via AnkiConnect (with KaTeX math, click-to-zoom images, keyboard shortcuts, and automatic AnkiWeb sync)
- **Scratchpad** that autosaves locally and appends directly to an Obsidian note (`⌘↩`)
- **Slow breathing dial** for stepping back from feeds without a stressful countdown
- **30-second emergency bypass gate** (server-enforced cooldown before allowing a 2-minute visit, e.g. for a 2FA email)
- **Auto-restore** that sends your tab back once all agents finish (pausing if you are mid-card or typing)

Zero dependencies beyond macOS Python 3, no `sudo`, no Xcode signing, and no system proxy conflicts.

## Quick start

```bash
git clone https://github.com/<your-username>/agent-focus.git
cd agent-focus
./install.sh
```

Run a 35-second interactive demo right away:

```bash
agent-focus demo
```

## CLI

```bash
agent-focus status        # Inspect active agent sessions and config
agent-focus demo [secs]   # Simulate a live agent run and open Safari
agent-focus open          # Open http://127.0.0.1:7842
agent-focus on | off      # Toggle always-block mode manually
agent-focus pause [mins]  # Pause Safari blocking temporarily
agent-focus resume        # Resume immediately
agent-focus clear         # Clear stuck active sessions
agent-focus restart       # Restart the background LaunchAgent
agent-focus logs          # Tail daemon logs
```

## Two-way Anki sync

1. Install **AnkiConnect** in Anki Desktop (`Tools -> Add-ons -> Get Add-ons... -> 2055492159`) and restart Anki.
2. Reviews submitted in Safari (`Space` to flip, `1`–`4` to rate) update Anki's real scheduler via `answerCards` and trigger a debounced AnkiWeb sync.
3. If Anki is closed, Agent Focus falls back to a safe read-only snapshot of your local collection without risking database locks.

## Configuration

Your personal config lives at `~/.local/share/agent-focus/config.json` (never committed to git). Edit domains directly in the **Settings** tab, or edit the JSON file:

- `blocked_domains`: hostnames to intercept in Safari (matches subdomains, e.g. `reddit.com` catches `old.reddit.com`; use `mail.proton.me` to block webmail without blocking other subdomains)
- `bypass_cooldown_seconds`: wait time before emergency bypass unlocks (default `30`)
- `default_bypass_seconds`: length of a temporary bypass (default `120`)
- `obsidian_note_path`: optional path to a Markdown file in your Obsidian vault for the `Append to Obsidian` action
- `anki_profile_dir`: path to your Anki profile folder

## Uninstall

```bash
./uninstall.sh
```

## License

MIT
