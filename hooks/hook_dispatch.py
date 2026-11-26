#!/usr/bin/env python3
"""Dispatches agent start/stop lifecycle events to the local Agent Focus daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request


def find_agent_pid(start_pid: int) -> int:
    pid = start_pid
    for _ in range(8):
        if pid <= 1:
            break
        try:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            if not out:
                break
            parts = out.split(None, 1)
            next_pid = int(parts[0])
            comm = parts[1].lower() if len(parts) > 1 else ""
            if any(k in comm for k in ("claude", "codex", "agy", "node", "orca-provider")):
                return pid
            pid = next_pid
        except Exception:
            break
    return 0


def main() -> None:
    if len(sys.argv) < 5:
        return
    agent = sys.argv[1]
    action = sys.argv[2]
    port = int(sys.argv[3])
    start_ppid = int(sys.argv[4]) if sys.argv[4].isdigit() else 0
    raw = sys.stdin.read()

    agent_pid = find_agent_pid(start_ppid)

    try:
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    fallback_id = f"{agent}-{agent_pid}" if agent_pid else f"{agent}-default"
    sid = (
        data.get("session_id")
        or data.get("conversationId")
        or data.get("sessionId")
        or data.get("thread_id")
        or os.environ.get("ORCA_PANE_KEY")
        or fallback_id
    )
    ws = data.get("workspacePaths")
    cwd = data.get("cwd") or (ws[0] if isinstance(ws, list) and ws else "") or os.getcwd()
    prompt = data.get("prompt") or data.get("lastUserInput") or data.get("user_prompt") or ""

    body = json.dumps(
        {
            "session_id": f"{agent}:{sid}",
            "agent": agent,
            "pid": agent_pid,
            "cwd": str(cwd),
            "prompt": str(prompt)[:160],
        }
    ).encode("utf-8")

    route = "start" if action == "start" else "stop"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/agent/{route}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=0.8).read()
    except Exception:
        pass


if __name__ == "__main__":
    main()
