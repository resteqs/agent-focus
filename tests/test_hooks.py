from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import support

sys.path.insert(0, str(support.ROOT / "hooks"))
import hook_dispatch  # noqa: E402

HOOKS_DIR = support.ROOT / "hooks"


class ParsePayloadTest(unittest.TestCase):
    def test_bad_or_non_object_json_becomes_empty(self):
        for raw in ("", "   ", "{not json", "[1, 2]", '"text"', "null"):
            with self.subTest(raw=raw):
                self.assertEqual(hook_dispatch.parse_payload(raw), {})

    def test_object_is_returned(self):
        self.assertEqual(hook_dispatch.parse_payload('{"session_id": "abc"}'), {"session_id": "abc"})


class BuildBodyTest(unittest.TestCase):
    def build(self, agent, data, pid=0, environ=None):
        return hook_dispatch.build_body(agent, data, pid, environ or {}, "/fallback/cwd")

    def test_claude_code_prompt_submit(self):
        data = {
            "session_id": "c0ffee",
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/work/agent-focus",
            "hook_event_name": "UserPromptSubmit",
            "prompt": "Add tests",
        }
        self.assertEqual(
            self.build("claude", data, pid=4242),
            {"session_id": "claude:c0ffee", "agent": "claude", "pid": 4242, "cwd": "/work/agent-focus", "prompt": "Add tests"},
        )

    def test_claude_code_stop_has_no_prompt(self):
        body = self.build("claude", {"session_id": "c0ffee", "cwd": "/work", "hook_event_name": "Stop"})
        self.assertEqual((body["session_id"], body["prompt"]), ("claude:c0ffee", ""))

    def test_codex(self):
        data = {"session_id": "019a-thread", "turn_id": "t1", "cwd": "/work/api", "prompt": "Fix the build"}
        body = self.build("codex", data)
        self.assertEqual((body["session_id"], body["cwd"], body["prompt"]), ("codex:019a-thread", "/work/api", "Fix the build"))

    def test_codex_thread_id(self):
        self.assertEqual(self.build("codex", {"thread_id": "th-9"})["session_id"], "codex:th-9")

    def test_antigravity(self):
        data = {
            "conversationId": "conv-77",
            "workspacePaths": ["/work/site", "/work/other"],
            "lastUserInput": "Refactor the header",
        }
        body = self.build("antigravity", data)
        self.assertEqual(
            (body["session_id"], body["cwd"], body["prompt"]),
            ("antigravity:conv-77", "/work/site", "Refactor the header"),
        )

    def test_antigravity_with_no_workspace_uses_fallback_cwd(self):
        self.assertEqual(self.build("antigravity", {"conversationId": "c", "workspacePaths": []})["cwd"], "/fallback/cwd")

    def test_session_id_falls_back_to_orca_pane_then_pid(self):
        self.assertEqual(self.build("codex", {}, environ={"ORCA_PANE_KEY": "pane-3"})["session_id"], "codex:pane-3")
        self.assertEqual(self.build("codex", {}, pid=99)["session_id"], "codex:codex-99")
        self.assertEqual(self.build("codex", {})["session_id"], "codex:codex-default")

    def test_prompt_is_truncated_and_stringified(self):
        self.assertEqual(len(self.build("claude", {"prompt": "x" * 400})["prompt"]), 160)
        self.assertEqual(self.build("claude", {"prompt": 42})["prompt"], "42")


class CapturingServer:
    """Records the JSON bodies the hook posts, in place of the daemon."""

    def __init__(self):
        self.requests: queue.Queue[tuple[str, dict]] = queue.Queue()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.put((self.path, json.loads(self.rfile.read(length))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HookProcessTest(unittest.TestCase):
    def setUp(self):
        self.daemon = CapturingServer()
        self.addCleanup(self.daemon.close)
        self.env = {k: v for k, v in os.environ.items() if k != "ORCA_PANE_KEY"}

    def test_dispatch_posts_start_and_stop(self):
        payload = json.dumps({"session_id": "s1", "cwd": "/work/demo", "prompt": "Ship it"})
        for action, route in (("start", "/api/agent/start"), ("stop", "/api/agent/stop")):
            with self.subTest(action=action):
                subprocess.run(
                    [sys.executable, str(HOOKS_DIR / "hook_dispatch.py"), "claude", action, str(self.daemon.port), "0"],
                    input=payload, text=True, check=True, env=self.env, timeout=10,
                )
                path, body = self.daemon.requests.get(timeout=5)
                self.assertEqual(path, route)
                self.assertEqual((body["session_id"], body["cwd"], body["prompt"]), ("claude:s1", "/work/demo", "Ship it"))

    def test_dispatch_survives_a_stopped_daemon(self):
        port = self.daemon.port
        self.daemon.close()
        result = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "hook_dispatch.py"), "codex", "start", str(port), "0"],
            input="{}", text=True, capture_output=True, env=self.env, timeout=10,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_shell_hook_answers_immediately_and_forwards(self):
        cases = (("claude", "start", "{}"), ("codex", "stop", "{}"), ("antigravity", "stop", '{"decision":""}'))
        for agent, action, expected in cases:
            with self.subTest(agent=agent, action=action):
                result = subprocess.run(
                    ["/bin/sh", str(HOOKS_DIR / "hook.sh"), agent, action],
                    input=json.dumps({"session_id": f"{agent}-1"}), text=True, capture_output=True,
                    env={**self.env, "AGENT_FOCUS_PORT": str(self.daemon.port)}, timeout=10,
                )
                self.assertEqual((result.returncode, result.stdout.strip()), (0, expected))
                path, body = self.daemon.requests.get(timeout=5)
                self.assertEqual((path, body["session_id"]), (f"/api/agent/{action}", f"{agent}:{agent}-1"))


if __name__ == "__main__":
    unittest.main()
