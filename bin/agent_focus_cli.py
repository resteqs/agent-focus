#!/usr/bin/env python3
"""Agent Focus command line for Windows (install.ps1 puts agent-focus.cmd on PATH for it).

macOS and Linux use the shell script bin/agent-focus. This file works there too, minus restart.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PORT = os.environ.get("AGENT_FOCUS_PORT", "7842")
BASE = f"http://127.0.0.1:{PORT}"
TASK_NAME = "AgentFocusDaemon"
USAGE = "Usage: agent-focus [status|anki [status|login [--endpoint URL]|sync|logout]|demo [secs]|on|off|clear|open|restart|logs]"


def data_dir() -> Path:
    if os.environ.get("AGENT_FOCUS_DATA_DIR"):
        return Path(os.environ["AGENT_FOCUS_DATA_DIR"])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "agent-focus"
    return Path.home() / ".local" / "share" / "agent-focus"


def call(path: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def error_of(e: urllib.error.HTTPError) -> str:
    try:
        return json.loads(e.read().decode("utf-8", errors="replace") or "{}").get("error") or str(e.reason)
    except ValueError:
        return str(e.reason)


def anki_login(args: list[str]) -> None:
    endpoint = args[args.index("--endpoint") + 1] if "--endpoint" in args[:-1] else ""
    email = input("AnkiWeb email or sync server username: ").strip()
    password = getpass.getpass("Password (not stored): ")
    step = "Sign-in failed"
    try:
        account = call("/api/anki/login", {"email": email, "password": password, "endpoint": endpoint}, 60)["account"]
        print(f"Signed in as {account['account']} on {account['endpoint_label']}. Downloading your decks...")
        step = "Sync failed"
        call("/api/anki/sync", {"media": False, "background_media": True, "on_conflict": "download"}, 600)
        print("Collection synced. Media is syncing in the background.")
    except urllib.error.HTTPError as e:
        sys.exit(f"{step}: {error_of(e)}")


def demo(secs: int) -> None:
    sid = f"claude:demo-{int(time.time())}"
    call("/api/agent/start", {"session_id": sid, "agent": "claude", "cwd": os.getcwd()})
    webbrowser.open(f"{BASE}/?from=https%3A%2F%2Fmail.proton.me&domain=mail.proton.me&t={int(time.time())}")
    print(f"Demo run started ({sid}) for {secs}s. Opened the wait-state screen.")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, __file__, "_demo-stop", sid, str(secs)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags if os.name == "nt" else 0, start_new_session=os.name != "nt",
    )


def restart() -> None:
    if os.name != "nt":
        sys.exit("restart is for Windows here. On macOS or Linux run: agent-focus restart (the shell script).")
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    result = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"Could not start the {TASK_NAME} task. Run install.ps1 first.")
    print("Restarted Agent Focus.")


def main(argv: list[str]) -> None:
    cmd, args = (argv[0], argv[1:]) if argv else ("status", [])
    try:
        if cmd == "status":
            print(json.dumps(call("/api/status"), indent=2))
        elif cmd in ("on", "off"):
            call("/api/focus", {"enabled": cmd == "on"})
            print(f"Always-block {cmd.upper()}.")
        elif cmd == "clear":
            call("/api/agent/clear", {})
            print("Cleared all active agent sessions.")
        elif cmd == "open":
            webbrowser.open(BASE)
        elif cmd == "demo":
            demo(int(args[0]) if args and args[0].isdigit() else 35)
        elif cmd == "_demo-stop":
            time.sleep(int(args[1]))
            call("/api/agent/stop", {"session_id": args[0], "agent": "claude"})
        elif cmd == "anki":
            sub = args[0] if args else "status"
            if sub == "status":
                print(json.dumps(call("/api/anki/account"), indent=2))
            elif sub == "login":
                anki_login(args[1:])
            elif sub == "sync":
                print(json.dumps(call("/api/anki/sync", {"media": False, "background_media": True}, 600), indent=2))
            elif sub == "logout":
                print(json.dumps(call("/api/anki/logout", {}), indent=2))
            else:
                print(USAGE)
        elif cmd == "restart":
            restart()
        elif cmd == "logs":
            log = data_dir() / "server.log"
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:] if log.exists() else ["No logs yet."]
            print("\n".join(lines))
        else:
            print(USAGE)
    except urllib.error.HTTPError as e:
        sys.exit(f"Agent Focus answered {e.code}: {error_of(e)}")
    except urllib.error.URLError:
        sys.exit("Agent Focus is not running. Start it with: agent-focus restart")


if __name__ == "__main__":
    main(sys.argv[1:])
