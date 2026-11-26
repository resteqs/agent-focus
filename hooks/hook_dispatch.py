#!/usr/bin/env python3
"""Forward an agent's start/stop hook to the local Agent Focus daemon.

Usage, hook JSON on stdin:
    hook_dispatch.py <agent> <start|stop> <port> <parent-pid>   (from hook.sh, which already answered the agent)
    hook_dispatch.py <agent> <start|stop>                       (direct, e.g. on Windows: answers the agent itself)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Any, Mapping

AGENT_PROCESS_NAMES = ("claude", "codex", "agy", "node", "orca-provider")


def hook_reply(agent: str, action: str) -> str:
    """What the agent expects on stdout. Antigravity's stop hook wants an explicit empty decision."""
    return '{"decision":""}' if agent == "antigravity" and action == "stop" else "{}"


def find_agent_pid(start_pid: int) -> int:
    """Walk up to 8 parents from the hook's shell to the long-lived agent process.

    Uses `ps`, so it finds nothing on Windows; sessions there end on stop hooks or the TTL.
    """
    if os.name == "nt":
        return 0
    pid = start_pid
    for _ in range(8):
        if pid <= 1:
            break
        try:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            break
        if not out:
            break
        ppid, _, comm = out.partition(" ")
        if any(name in comm.lower() for name in AGENT_PROCESS_NAMES):
            return pid
        pid = int(ppid)
    return 0


def parse_payload(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def build_body(
    agent: str,
    data: Mapping[str, Any],
    agent_pid: int,
    environ: Mapping[str, str],
    fallback_cwd: str,
) -> dict[str, Any]:
    """Map Claude Code, Codex, and Antigravity hook payloads onto one session record."""
    sid = (
        data.get("session_id")  # Claude Code, Codex
        or data.get("conversationId")  # Antigravity
        or data.get("sessionId")
        or data.get("thread_id")
        or environ.get("ORCA_PANE_KEY")
        or (f"{agent}-{agent_pid}" if agent_pid else f"{agent}-default")
    )
    workspaces = data.get("workspacePaths")
    cwd = data.get("cwd") or (workspaces[0] if isinstance(workspaces, list) and workspaces else "") or fallback_cwd
    prompt = data.get("prompt") or data.get("lastUserInput") or data.get("user_prompt") or ""
    return {
        "session_id": f"{agent}:{sid}",
        "agent": agent,
        "pid": agent_pid,
        "cwd": str(cwd),
        "prompt": str(prompt)[:160],
    }


def main() -> None:
    if len(sys.argv) < 3:
        return
    agent, action = sys.argv[1], sys.argv[2]
    direct = len(sys.argv) < 5
    port_arg = sys.argv[3] if not direct else os.environ.get("AGENT_FOCUS_PORT", "7842")
    port = int(port_arg) if port_arg.isdigit() else 7842
    start_ppid = sys.argv[4] if not direct else str(os.getppid())
    raw = sys.stdin.read() if sys.stdin is not None else ""
    if direct:
        print(hook_reply(agent, action), flush=True)
    data = parse_payload(raw)
    agent_pid = find_agent_pid(int(start_ppid) if start_ppid.isdigit() else 0)
    body = build_body(agent, data, agent_pid, os.environ, os.getcwd())

    route = "start" if action == "start" else "stop"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/agent/{route}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=0.8).read()
    except OSError:
        pass  # daemon not running; the agent must never wait on us


if __name__ == "__main__":
    main()
