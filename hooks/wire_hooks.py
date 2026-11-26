#!/usr/bin/env python3
"""Add or remove the Agent Focus hooks in Claude Code, Codex, and Antigravity configs.

    wire_hooks.py install <command prefix>             e.g. /path/hooks/hook.sh (macOS, Linux)
    wire_hooks.py install-direct <python> <dispatch>   Windows: "<python>" "<hook_dispatch.py>", no bash needed
    wire_hooks.py uninstall

Each hook runs "<prefix> <agent> <start|stop>". Only configs that already exist are touched,
and installing twice repoints the existing entries instead of adding duplicates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HOOK_FILES = ("hook.sh", "hook_dispatch.py")

# (config path under home, event, agent, action, extra handler fields)
GROUP_HOOKS = {
    ".claude/settings.json": [
        ("UserPromptSubmit", "claude", "start", {"async": True}),
        ("Stop", "claude", "stop", None),
        ("StopFailure", "claude", "stop", None),
        ("SessionEnd", "claude", "stop", None),
    ],
    ".codex/hooks.json": [
        ("UserPromptSubmit", "codex", "start", None),
        ("Stop", "codex", "stop", None),
        ("Interrupt", "codex", "stop", None),
    ],
}
ANTIGRAVITY = ".gemini/config/hooks.json"


def is_ours(command: str) -> bool:
    return "agent-focus" in command or any(name in command for name in HOOK_FILES)


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    return data if isinstance(data, dict) else {}


def save_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def replace_or_add_group(hooks_map: dict[str, Any], event: str, cmd: str, suffix: str, extra: dict | None) -> None:
    entries = hooks_map.setdefault(event, [])
    # Repoint an existing Agent Focus hook (moved checkout, new Python) instead of adding a second one.
    for entry in entries:
        for handler in entry.get("hooks", []):
            existing = str(handler.get("command", ""))
            if is_ours(existing) and existing.endswith(suffix):
                handler["command"] = cmd
                return
    handler = {"type": "command", "command": cmd, "timeout": 5, **(extra or {})}
    entries.append({"hooks": [handler]})


def install(prefix: str, home: Path) -> list[str]:
    wired = []
    for rel, hooks in GROUP_HOOKS.items():
        path = home / rel
        if not path.exists():
            continue
        data = load(path)
        hooks_map = data.setdefault("hooks", {})
        for event, agent, action, extra in hooks:
            replace_or_add_group(hooks_map, event, f"{prefix} {agent} {action}", f" {agent} {action}", extra)
        save_atomic(path, data)
        wired.append(str(path))
    path = home / ANTIGRAVITY
    if path.exists():
        data = load(path)
        data["agent-focus"] = {
            "PreInvocation": [{"type": "command", "command": f"{prefix} antigravity start", "timeout": 5}],
            "Stop": [{"type": "command", "command": f"{prefix} antigravity stop", "timeout": 5}],
        }
        save_atomic(path, data)
        wired.append(str(path))
    return wired


def uninstall(home: Path) -> None:
    for rel in GROUP_HOOKS:
        path = home / rel
        if not path.exists():
            continue
        data = load(path)
        hooks_map = data.get("hooks", {})
        for event, entries in list(hooks_map.items()):
            hooks_map[event] = [
                e for e in entries if not any(is_ours(str(h.get("command", ""))) for h in e.get("hooks", []))
            ]
        save_atomic(path, data)
    path = home / ANTIGRAVITY
    if path.exists():
        data = load(path)
        data.pop("agent-focus", None)
        save_atomic(path, data)


def direct_prefix(python: str, dispatch: str) -> str:
    """Quoted, forward-slash paths: valid in cmd.exe, PowerShell, and Git Bash alike."""
    return " ".join(f'"{p.replace(chr(92), "/")}"' for p in (python, dispatch))


def main(argv: list[str]) -> int:
    if len(argv) >= 4 and argv[1] == "install-direct":
        argv = [argv[0], "install", direct_prefix(argv[2], argv[3])]
    if len(argv) >= 3 and argv[1] == "install":
        for path in install(argv[2], Path.home()):
            print(f"Wired agent hooks: {path}")
        return 0
    if len(argv) >= 2 and argv[1] == "uninstall":
        uninstall(Path.home())
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
