"""Cross-platform branches (Windows and Linux are mocked), the browser extension, and the installers' hook wiring."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support

import ankistore
import server
from test_hooks import CapturingServer
from test_server import isolated_state

HOOKS_DIR = support.ROOT / "hooks"
EXTENSION = support.ROOT / "extension"
NODE = shutil.which("node")
sys.path.insert(0, str(HOOKS_DIR))
import wire_hooks  # noqa: E402


class DataPathTest(unittest.TestCase):
    def test_windows_data_root_is_localappdata(self):
        with mock.patch.object(ankistore, "IS_WINDOWS", True), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": "/fake/AppData/Local"}):
            self.assertEqual(ankistore.default_data_root(), Path("/fake/AppData/Local"))

    def test_posix_data_root_is_local_share(self):
        with mock.patch.object(ankistore, "IS_WINDOWS", False):
            self.assertEqual(ankistore.default_data_root(), Path.home() / ".local" / "share")

    def test_store_home_follows_the_data_root(self):
        root = Path(tempfile.mkdtemp(dir=support.SANDBOX))
        with mock.patch.dict(os.environ), mock.patch.object(ankistore, "default_data_root", return_value=root):
            os.environ.pop("ANKI_HEADLESS_HOME", None)
            store = ankistore.AnkiStore()
        self.assertEqual(store.home, root / "anki-headless" / "agent-focus")

    def test_private_file_on_windows_is_a_plain_write(self):
        path = Path(tempfile.mkdtemp(dir=support.SANDBOX)) / "auth.json"
        with mock.patch.object(ankistore, "IS_WINDOWS", True), mock.patch.object(ankistore.os, "open") as os_open:
            ankistore.write_private(path, "{}\n")
        os_open.assert_not_called()
        self.assertEqual(path.read_text(), "{}\n")

    def test_private_file_on_posix_is_0600(self):
        path = Path(tempfile.mkdtemp(dir=support.SANDBOX)) / "auth.json"
        ankistore.write_private(path, "secret")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_file_lock_is_exclusive_and_released(self):
        lock_path = Path(tempfile.mkdtemp(dir=support.SANDBOX)) / ".lock"
        probe = (
            "import fcntl, sys\n"
            "f = open(sys.argv[1], 'w')\n"
            "try:\n    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); print('free')\n"
            "except OSError:\n    print('held')\n"
        )
        check = lambda: subprocess.run([sys.executable, "-c", probe, str(lock_path)], capture_output=True, text=True).stdout.strip()
        with open(lock_path, "w") as f, ankistore._file_lock(f):
            self.assertEqual(check(), "held")
        self.assertEqual(check(), "free")


class PidAliveTest(unittest.TestCase):
    def fake_kernel32(self, handle: int, exit_code: int):
        kernel32 = mock.Mock()
        kernel32.OpenProcess.return_value = handle

        def get_exit_code(_handle, ref):
            ref._obj.value = exit_code
            return 1

        kernel32.GetExitCodeProcess.side_effect = get_exit_code
        return kernel32

    def check_windows(self, kernel32, last_error: int = 0) -> bool:
        windll = mock.Mock(kernel32=kernel32)
        with mock.patch.object(server, "PLATFORM", "win32"), \
                mock.patch.object(ctypes, "windll", windll, create=True), \
                mock.patch.object(ctypes, "GetLastError", return_value=last_error, create=True):
            return server.pid_alive(4242)

    def test_windows_running_process(self):
        kernel32 = self.fake_kernel32(handle=7, exit_code=259)
        self.assertTrue(self.check_windows(kernel32))
        kernel32.OpenProcess.assert_called_once_with(0x1000, False, 4242)
        kernel32.CloseHandle.assert_called_once_with(7)

    def test_windows_exited_process(self):
        kernel32 = self.fake_kernel32(handle=7, exit_code=0)
        self.assertFalse(self.check_windows(kernel32))
        kernel32.CloseHandle.assert_called_once_with(7)

    def test_windows_missing_or_foreign_process(self):
        self.assertFalse(self.check_windows(self.fake_kernel32(handle=0, exit_code=0), last_error=87))
        self.assertTrue(self.check_windows(self.fake_kernel32(handle=0, exit_code=0), last_error=5))

    def test_posix_uses_signal_zero(self):
        self.assertTrue(server.pid_alive(os.getpid()))


class FocusAppTest(unittest.TestCase):
    def setUp(self):
        isolated_state(self)

    def post(self, platform: str, which=lambda _name: None):
        with mock.patch.object(server, "PLATFORM", platform), \
                mock.patch.object(server.shutil, "which", side_effect=which), \
                mock.patch.object(server.subprocess, "Popen") as popen:
            status, data = server.post_focus_app({"app": "Orca"}, {})
        return status, data, popen

    def test_macos_uses_open(self):
        status, _, popen = self.post("darwin")
        self.assertEqual(status, 200)
        popen.assert_called_once_with(["/usr/bin/open", "-a", "Orca"], env=None)

    def test_windows_passes_the_name_through_the_environment(self):
        status, _, popen = self.post("win32")
        self.assertEqual(status, 200)
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertEqual(argv[0], "powershell")
        self.assertIn("$env:AGENT_FOCUS_APP", argv[-1])
        self.assertNotIn("Orca", " ".join(argv))  # the name never reaches the command line
        self.assertEqual(env["AGENT_FOCUS_APP"], "Orca")

    def test_linux_prefers_wmctrl_then_xdotool(self):
        _, _, popen = self.post("linux", which=lambda name: f"/usr/bin/{name}")
        popen.assert_called_once_with(["wmctrl", "-a", "Orca"], env=None)
        _, _, popen = self.post("linux", which=lambda name: "/usr/bin/xdotool" if name == "xdotool" else None)
        popen.assert_called_once_with(["xdotool", "search", "--name", "Orca", "windowactivate"], env=None)

    def test_linux_without_tools_explains_what_to_install(self):
        status, data, popen = self.post("linux")
        self.assertEqual((status, data["ok"]), (501, False))
        self.assertIn("wmctrl or xdotool", data["error"])
        popen.assert_not_called()

    def test_invalid_names_never_run_anything(self):
        for platform in ("darwin", "win32", "linux"):
            with self.subTest(platform=platform), mock.patch.object(server, "PLATFORM", platform), \
                    mock.patch.object(server.subprocess, "Popen") as popen:
                with self.assertRaises(server.BadRequest):
                    server.post_focus_app({"app": "Orca'; Stop-Computer; '"}, {})
                popen.assert_not_called()


class WatcherTest(unittest.TestCase):
    def test_safari_polling_is_macos_only(self):
        state = isolated_state(self)
        for platform, expected in (("darwin", True), ("win32", False), ("linux", False)):
            with self.subTest(platform=platform), mock.patch.object(server, "PLATFORM", platform):
                self.assertIs(server.SafariWatcher(state).poll_safari, expected)

    def test_without_safari_the_watcher_never_runs_osascript(self):
        state = isolated_state(self)
        state.set_manual_focus(True)
        watcher = server.SafariWatcher(state, poll_safari=False)
        with mock.patch.object(watcher, "_run_osascript") as osa, \
                mock.patch.object(watcher.stop_event, "wait", side_effect=lambda _t: watcher.stop_event.set()):
            watcher.run()
        osa.assert_not_called()


class DirectHookTest(unittest.TestCase):
    """Windows runs hook_dispatch.py directly, without hook.sh, so it must answer the agent itself."""

    def test_direct_mode_answers_and_forwards(self):
        daemon = CapturingServer()
        self.addCleanup(daemon.close)
        env = {k: v for k, v in os.environ.items() if k != "ORCA_PANE_KEY"}
        env["AGENT_FOCUS_PORT"] = str(daemon.port)
        for agent, action, expected in (("claude", "start", "{}"), ("antigravity", "stop", '{"decision":""}')):
            with self.subTest(agent=agent):
                result = subprocess.run(
                    [sys.executable, str(HOOKS_DIR / "hook_dispatch.py"), agent, action],
                    input=json.dumps({"session_id": "w1"}), text=True, capture_output=True, env=env, timeout=10,
                )
                self.assertEqual((result.returncode, result.stdout.strip()), (0, expected))
                path, body = daemon.requests.get(timeout=5)
                self.assertEqual((path, body["session_id"]), (f"/api/agent/{action}", f"{agent}:w1"))

    def test_parent_walk_is_skipped_on_windows(self):
        import hook_dispatch

        with mock.patch.object(hook_dispatch.os, "name", "nt"), mock.patch.object(hook_dispatch.subprocess, "check_output") as ps:
            self.assertEqual(hook_dispatch.find_agent_pid(1234), 0)
        ps.assert_not_called()


class WireHooksTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=support.SANDBOX))
        for rel, data in ((".claude/settings.json", {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-tool"}]}]}}),
                          (".codex/hooks.json", {}), (".gemini/config/hooks.json", {"keep": 1})):
            path = self.home / rel
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(data))

    def read(self, rel: str) -> dict:
        return json.loads((self.home / rel).read_text())

    def commands(self, rel: str) -> list[str]:
        hooks = self.read(rel)["hooks"]
        return [h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]]

    def test_install_is_idempotent_and_repoints(self):
        wire_hooks.install("/old/agent-focus/hooks/hook.sh", self.home)
        wire_hooks.install("/new/agent-focus/hooks/hook.sh", self.home)
        claude = self.commands(".claude/settings.json")
        self.assertIn("other-tool", claude)
        self.assertEqual(sorted(c for c in claude if c != "other-tool"), sorted([
            "/new/agent-focus/hooks/hook.sh claude start",
            "/new/agent-focus/hooks/hook.sh claude stop",
            "/new/agent-focus/hooks/hook.sh claude stop",
            "/new/agent-focus/hooks/hook.sh claude stop",
        ]))
        self.assertTrue(self.read(".claude/settings.json")["hooks"]["UserPromptSubmit"][0]["hooks"][0]["async"])
        self.assertEqual(len(self.commands(".codex/hooks.json")), 3)
        agy = self.read(".gemini/config/hooks.json")
        self.assertEqual((agy["keep"], agy["agent-focus"]["Stop"][0]["command"]), (1, "/new/agent-focus/hooks/hook.sh antigravity stop"))

    def test_windows_prefix_is_quoted_with_forward_slashes(self):
        prefix = wire_hooks.direct_prefix(r"D:\Home\A B\AppData\Local\agent-focus\venv\Scripts\python.exe", r"C:\src\focus\hooks\hook_dispatch.py")
        self.assertEqual(prefix, '"D:/Home/A B/AppData/Local/agent-focus/venv/Scripts/python.exe" "C:/src/focus/hooks/hook_dispatch.py"')
        wire_hooks.install(prefix, self.home)
        wire_hooks.install(prefix.replace("focus", "moved"), self.home)  # a moved checkout repoints, it doesn't duplicate
        self.assertEqual(len(self.commands(".codex/hooks.json")), 3)

    def test_uninstall_keeps_other_hooks(self):
        wire_hooks.install("/x/agent-focus/hooks/hook.sh", self.home)
        wire_hooks.uninstall(self.home)
        self.assertEqual(self.commands(".claude/settings.json"), ["other-tool"])
        self.assertEqual(self.commands(".codex/hooks.json"), [])
        self.assertEqual(self.read(".gemini/config/hooks.json"), {"keep": 1})

    def test_missing_configs_are_left_alone(self):
        empty = Path(tempfile.mkdtemp(dir=support.SANDBOX))
        self.assertEqual(wire_hooks.install("/x/hook.sh", empty), [])
        wire_hooks.uninstall(empty)
        self.assertEqual(list(empty.iterdir()), [])


class ExtensionManifestTest(unittest.TestCase):
    def test_manifest(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text())
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["name"], "Agent Focus")
        self.assertEqual(manifest["permissions"], ["tabs", "alarms"])
        self.assertEqual(manifest["host_permissions"], ["http://127.0.0.1:7842/*"])
        self.assertEqual(manifest["background"], {"service_worker": "background.js", "scripts": ["background.js"]})
        self.assertEqual(manifest["browser_specific_settings"]["gecko"]["id"], "agent-focus@resteqs.github.io")
        for icon in manifest["icons"].values():
            self.assertTrue((EXTENSION / icon).read_bytes().startswith(b"\x89PNG"))


@unittest.skipUnless(NODE, "node is not installed")
class ExtensionScriptTest(unittest.TestCase):
    HARNESS = r"""
const fs = require("fs");
const calls = { updates: [], intercepts: [] };
const listeners = {};
const on = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
const STATE = JSON.parse(process.argv[2]);
globalThis.fetch = async (url, opts = {}) => {
  if (url.endsWith("/api/status")) return { ok: true, json: async () => STATE };
  const body = JSON.parse(opts.body);
  calls.intercepts.push(body.url);
  return { ok: true, json: async () => ({ blocked: true, redirect_url: "http://127.0.0.1:7842/?from=x" }) };
};
globalThis.chrome = {
  tabs: {
    onUpdated: on("updated"), onActivated: on("activated"),
    update: async (id, props) => { calls.updates.push([id, props.url]); },
    get: async (id) => ({ id, url: "" }), query: async () => [],
  },
  alarms: { create: () => {}, onAlarm: on("alarm") },
  runtime: { onStartup: on("startup"), onInstalled: on("installed") },
};
eval(fs.readFileSync(process.argv[1], "utf8"));
(async () => {
  const urls = JSON.parse(process.argv[3]);
  urls.forEach((u, i) => listeners.updated(i + 1, { url: u }, { url: u }));
  await new Promise((r) => setTimeout(r, 50));
  console.log(JSON.stringify(calls));
})();
"""

    def run_extension(self, state: dict, urls: list[str]) -> dict:
        out = subprocess.run(
            [NODE, "-e", self.HARNESS, str(EXTENSION / "background.js"), json.dumps(state), json.dumps(urls)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return json.loads(out.stdout)

    def test_background_script_parses(self):
        subprocess.run([NODE, "--check", str(EXTENSION / "background.js")], check=True, capture_output=True)

    def test_blocked_tabs_are_sent_to_the_wait_page(self):
        state = {"blocking": True, "bypasses": {"x.com": 90}, "config": {"blocked_domains": ["reddit.com", "x.com"]}}
        calls = self.run_extension(state, [
            "https://old.reddit.com/r/python", "https://x.com/home", "https://example.com/",
            "http://127.0.0.1:7842/?from=https%3A%2F%2Freddit.com", "chrome://extensions",
        ])
        self.assertEqual(calls["intercepts"], ["https://old.reddit.com/r/python"])
        self.assertEqual(calls["updates"], [[1, "http://127.0.0.1:7842/?from=x"]])

    def test_nothing_happens_when_idle(self):
        calls = self.run_extension({"blocking": False, "config": {"blocked_domains": ["reddit.com"]}}, ["https://reddit.com/"])
        self.assertEqual(calls, {"updates": [], "intercepts": []})


if __name__ == "__main__":
    unittest.main()
