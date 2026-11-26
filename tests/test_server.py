from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import support

import server

HOME_DATA = Path.home() / ".local/share"


def isolated_state(test: unittest.TestCase) -> server.FocusState:
    """A fresh FocusState whose files live in a new directory under the sandbox."""
    data_dir = Path(tempfile.mkdtemp(dir=support.SANDBOX))
    support.write_config(data_dir)
    patcher = mock.patch.multiple(
        server,
        DATA_DIR=data_dir,
        CONFIG_PATH=data_dir / "config.json",
        SCRATCHPAD_PATH=data_dir / "scratchpad.md",
        ANKI_PROGRESS_PATH=data_dir / "anki_progress.json",
    )
    patcher.start()
    test.addCleanup(patcher.stop)
    quiet = mock.patch.object(server, "log")
    quiet.start()
    test.addCleanup(quiet.stop)
    state = server.FocusState()
    state_patch = mock.patch.object(server, "STATE", state)
    state_patch.start()
    test.addCleanup(state_patch.stop)
    return state


class SandboxTest(unittest.TestCase):
    def test_data_paths_never_point_at_the_real_home(self):
        for path in (server.DATA_DIR, server.STATE.anki_store.home, server.STATE._anki_profile_dir()):
            self.assertTrue(str(path).startswith(str(support.SANDBOX)), path)
            self.assertFalse(str(path).startswith(str(HOME_DATA)), path)


class DomainTest(unittest.TestCase):
    def test_normalize_domain(self):
        cases = {
            "https://www.Reddit.com/r/python": "reddit.com",
            "old.reddit.com:443/path": "old.reddit.com",
            "  WWW.example.org. ": "example.org",
            "mail.proton.me": "mail.proton.me",
            "": "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(server.normalize_domain(raw), expected)

    def test_normalize_domains_dedupes_and_drops_empty(self):
        self.assertEqual(
            server.normalize_domains(["x.com", "https://www.x.com/", "", "  ", "bbc.com"]),
            ["x.com", "bbc.com"],
        )

    def test_match_blocked_domain(self):
        state = isolated_state(self)
        state.config["blocked_domains"] = ["reddit.com", "mail.proton.me"]
        self.assertEqual(state.match_blocked_domain("https://old.reddit.com/r/x"), "reddit.com")
        self.assertEqual(state.match_blocked_domain("https://www.reddit.com/"), "reddit.com")
        self.assertEqual(state.match_blocked_domain("https://mail.proton.me/u/0"), "mail.proton.me")
        self.assertIsNone(state.match_blocked_domain("https://notreddit.com/"))
        self.assertIsNone(state.match_blocked_domain("https://account.proton.me/"))
        self.assertIsNone(state.match_blocked_domain("file:///reddit.com"))
        self.assertIsNone(state.match_blocked_domain("http://127.0.0.1:7842/?from=reddit.com"))
        self.assertIsNone(state.match_blocked_domain(""))

    def test_active_bypass_unblocks_domain_and_subdomains(self):
        state = isolated_state(self)
        state.config["blocked_domains"] = ["reddit.com"]
        state.bypasses["reddit.com"] = time.time() + 60
        self.assertIsNone(state.match_blocked_domain("https://old.reddit.com/"))
        state.bypasses["reddit.com"] = time.time() - 1
        self.assertEqual(state.match_blocked_domain("https://old.reddit.com/"), "reddit.com")


class SessionTest(unittest.TestCase):
    def test_start_and_stop_toggle_blocking(self):
        state = isolated_state(self)
        snap = state.start_session("claude:1", "claude", cwd="/tmp/project-a", prompt="fix it")
        self.assertTrue(snap["blocking"])
        self.assertEqual(snap["active_count"], 1)
        self.assertEqual(snap["agents"][0]["project"], "project-a")
        self.assertFalse(state.stop_session("claude:1")["blocking"])

    def test_restart_updates_the_session_in_place(self):
        state = isolated_state(self)
        state.start_session("codex:1", "codex", prompt="first")
        snap = state.start_session("codex:1", "codex", prompt="second")
        self.assertEqual(snap["active_count"], 1)
        self.assertEqual(snap["agents"][0]["prompt"], "second")

    def test_prompt_is_truncated(self):
        state = isolated_state(self)
        snap = state.start_session("claude:1", "claude", prompt="x" * 500)
        self.assertEqual(len(snap["agents"][0]["prompt"]), 160)

    def test_sessions_older_than_ttl_are_pruned(self):
        state = isolated_state(self)
        state.start_session("claude:old", "claude")
        state.sessions["claude:old"]["updated_at"] = time.time() - state.config["session_ttl_seconds"] - 1
        self.assertEqual(state.snapshot()["active_count"], 0)

    def test_sessions_of_dead_processes_are_pruned(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        state = isolated_state(self)
        state.start_session("claude:dead", "claude", pid=proc.pid)
        state.start_session("claude:alive", "claude", pid=os.getpid())
        self.assertEqual([a["session_id"] for a in state.snapshot()["agents"]], ["claude:alive"])

    def test_clear_drops_sessions_and_always_block(self):
        state = isolated_state(self)
        state.start_session("claude:1", "claude")
        state.set_manual_focus(True)
        snap = state.clear_sessions()
        self.assertEqual(snap["active_count"], 0)
        self.assertFalse(snap["manual_focus"])
        self.assertFalse(snap["blocking"])

    def test_pause_and_resume(self):
        state = isolated_state(self)
        state.start_session("claude:1", "claude")
        paused = state.set_pause(600)
        self.assertFalse(paused["blocking"])
        self.assertGreater(paused["paused_remaining"], 590)
        self.assertFalse(state.is_blocking_active())
        resumed = state.set_pause(0)
        self.assertTrue(resumed["blocking"])
        self.assertEqual(resumed["paused_remaining"], 0)

    def test_expired_pause_resumes_on_its_own(self):
        state = isolated_state(self)
        state.set_manual_focus(True)
        state.paused_until = time.time() - 1
        self.assertTrue(state.is_blocking_active())

    def test_intercepts_are_counted_per_day(self):
        state = isolated_state(self)
        state.record_intercept("reddit.com", "https://reddit.com/")
        state.record_intercept("reddit.com", "https://reddit.com/")
        self.assertEqual(state.snapshot()["intercept_count_today"], 2)
        state.intercept_day = "2000-01-01"
        self.assertEqual(state.snapshot()["intercept_count_today"], 0)


class StudyXpTest(unittest.TestCase):
    def award(self, state: server.FocusState, ease: str) -> dict[str, int]:
        with state.lock:
            return state._award_study_xp_locked(ease)

    def test_xp_and_combo(self):
        state = isolated_state(self)
        self.assertEqual(self.award(state, "good")["gained_xp"], 15)
        self.assertEqual(self.award(state, "hard")["gained_xp"], 10)
        third = self.award(state, "easy")
        self.assertEqual((third["gained_xp"], third["combo"]), (25, 3))
        again = self.award(state, "again")
        self.assertEqual((again["gained_xp"], again["combo"]), (0, 0))
        self.assertEqual(again["xp_today"], 50)

    def test_progress_is_persisted(self):
        state = isolated_state(self)
        self.award(state, "good")
        saved = json.loads(server.ANKI_PROGRESS_PATH.read_text())
        self.assertEqual((saved["xp_today"], saved["combo"]), (15, 1))

    def test_daily_reset(self):
        state = isolated_state(self)
        state.anki_progress.update(day="2000-01-01", xp_today=99, combo=7, reviewed_today=12)
        with state.lock:
            stats = state._study_stats_locked()
        self.assertEqual((stats["xp_today"], stats["combo"]), (0, 0))
        self.assertEqual(state.anki_progress["reviewed_today"], 0)

    def test_streak_continues_from_yesterday(self):
        state = isolated_state(self)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        state.anki_progress.update(last_study_day=yesterday, streak_days=4)
        self.assertEqual(self.award(state, "good")["streak_days"], 5)
        self.assertEqual(self.award(state, "good")["streak_days"], 5)

    def test_streak_restarts_after_a_gap(self):
        state = isolated_state(self)
        state.anki_progress.update(last_study_day=(date.today() - timedelta(days=3)).isoformat(), streak_days=9)
        self.assertEqual(self.award(state, "good")["streak_days"], 1)

    def test_broken_streak_is_not_shown_before_the_first_review(self):
        state = isolated_state(self)
        state.anki_progress.update(last_study_day=(date.today() - timedelta(days=3)).isoformat(), streak_days=9)
        with state.lock:
            self.assertEqual(state._study_stats_locked()["streak_days"], 0)


class HeadlessSyncFlagTest(unittest.TestCase):
    def test_failed_sync_keeps_reviews_pending(self):
        state = isolated_state(self)
        state._anki_dirty = True
        with mock.patch.object(state.anki_store, "is_authenticated", return_value=True), \
                mock.patch.object(state.anki_store, "sync", side_effect=OSError("offline")):
            ok, info = state.flush_anki_sync(only_if_dirty=True, media=False)
        self.assertFalse(ok)
        self.assertEqual(info["error"], "offline")
        self.assertTrue(state._anki_dirty)

    def test_background_flush_never_calls_ankiconnect_sync(self):
        state = isolated_state(self)
        state._anki_dirty = True
        with mock.patch.object(server, "ankiconnect") as connect:
            self.assertEqual(state.flush_anki_sync(only_if_dirty=True), (True, {"skipped": True}))
        connect.assert_not_called()


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.FocusHTTPServer((server.HOST, 0), server.RequestHandler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.state = isolated_state(self)

    def request(self, method: str, path: str, body: object = None, headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection(server.HOST, self.port, timeout=5)
        payload = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw
        return resp.status, data

    def post(self, path: str, body: object = None, **kw):
        return self.request("POST", path, {} if body is None else body, **kw)

    def test_status_and_index(self):
        status, data = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(data["blocking"])
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Agent Focus</title>", page)

    def test_agent_start_stop_clear(self):
        status, snap = self.post("/api/agent/start", {"session_id": "claude:a", "agent": "claude", "cwd": "/tmp/demo"})
        self.assertEqual((status, snap["active_count"]), (200, 1))
        self.post("/api/agent/start", {"session_id": "codex:b", "agent": "codex"})
        _, snap = self.post("/api/agent/stop", {"session_id": "claude:a"})
        self.assertEqual([a["session_id"] for a in snap["agents"]], ["codex:b"])
        _, snap = self.post("/api/agent/clear")
        self.assertEqual(snap["active_count"], 0)

    def test_pause_and_resume(self):
        self.post("/api/focus", {"enabled": True})
        _, snap = self.post("/api/pause", {"seconds": 600})
        self.assertFalse(snap["blocking"])
        _, snap = self.post("/api/pause", {"seconds": 0})
        self.assertTrue(snap["blocking"])

    def test_bypass_is_locked_until_the_cooldown_passes(self):
        status, data = self.post("/api/bypass", {"domain": "reddit.com", "seconds": 120})
        self.assertEqual(status, 429)
        self.assertEqual(data["remaining"], 30)

        self.state.intercept_started["reddit.com"] -= 10
        status, data = self.post("/api/bypass", {"domain": "reddit.com"})
        self.assertEqual((status, data["remaining"]), (429, 20))

        self.state.intercept_started["reddit.com"] -= 21
        status, data = self.post("/api/bypass", {"domain": "https://www.reddit.com/r/x"})
        self.assertEqual(status, 200)
        self.assertGreater(data["state"]["bypasses"]["reddit.com"], 100)
        self.assertNotIn("reddit.com", data["state"]["bypass_cooldowns"])

        _, snap = self.post("/api/bypass", {"domain": "reddit.com", "remove": True})
        self.assertEqual(snap["bypasses"], {})

    def test_bypass_cooldown_starts_at_the_intercept(self):
        self.state.record_intercept("x.com", "https://x.com/")
        self.state.intercept_started["x.com"] -= 31
        status, _ = self.post("/api/bypass", {"domain": "x.com"})
        self.assertEqual(status, 200)

    def test_bypass_requires_a_domain(self):
        self.assertEqual(self.post("/api/bypass", {"domain": ""})[0], 400)

    def test_config_add_and_remove_domain(self):
        _, snap = self.post("/api/config", {"add_domain": "https://www.Example.com/feed"})
        self.assertEqual(snap["config"]["blocked_domains"][0], "example.com")
        count = len(snap["config"]["blocked_domains"])
        _, snap = self.post("/api/config", {"add_domain": "example.com"})
        self.assertEqual(len(snap["config"]["blocked_domains"]), count)
        saved = json.loads(server.CONFIG_PATH.read_text())
        self.assertIn("example.com", saved["blocked_domains"])

        _, snap = self.post("/api/config", {"remove_domain": "www.example.com"})
        self.assertNotIn("example.com", snap["config"]["blocked_domains"])
        self.assertNotIn("example.com", json.loads(server.CONFIG_PATH.read_text())["blocked_domains"])

    def test_config_toggles_and_bounds(self):
        _, snap = self.post(
            "/api/config",
            {"auto_restore": True, "completion_chime": False, "default_bypass_seconds": 1, "bypass_cooldown_seconds": -5},
        )
        cfg = snap["config"]
        self.assertEqual(
            (cfg["auto_restore"], cfg["completion_chime"], cfg["default_bypass_seconds"], cfg["bypass_cooldown_seconds"]),
            (True, False, 15, 0),
        )

    def test_scratchpad_round_trip(self):
        self.post("/api/scratchpad", {"text": "next prompt"})
        self.assertEqual(self.request("GET", "/api/scratchpad")[1], {"text": "next prompt"})

    def test_obsidian_append(self):
        note = Path(tempfile.mkdtemp(dir=support.SANDBOX)) / "vault" / "Agent.md"
        self.state.config["obsidian_note_path"] = str(note)
        self.assertEqual(self.post("/api/scratchpad/obsidian", {"text": "   "})[0], 400)
        status, data = self.post("/api/scratchpad/obsidian", {"text": "first"})
        self.assertEqual((status, data["ok"]), (200, True))
        self.post("/api/scratchpad/obsidian", {"text": "second"})
        text = note.read_text()
        self.assertTrue(text.startswith("# Agent Scratchpad\n"))
        self.assertLess(text.index("first"), text.index("second"))

    def test_offline_review_awards_xp(self):
        status, data = self.post("/api/anki/review", {"card_id": "1234", "ease": "good"})
        self.assertEqual(status, 200)
        self.assertEqual((data["source"], data["synced"], data["reviewed_today"], data["gained_xp"]), ("offline", False, 1, 15))
        self.assertEqual(self.state.anki_progress["cards"]["1234"]["last_ease"], "good")

    def test_review_input_is_validated(self):
        for body in ({"card_id": "", "ease": "good"}, {"card_id": "12; DROP", "ease": "good"}, {"card_id": "12", "ease": "meh"}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/anki/review", body)[0], 400)
        self.assertEqual(self.state.anki_progress["xp_today"], 0)

    def test_cards_fall_back_to_empty_offline_payload(self):
        status, data = self.request("GET", "/api/anki/cards?deck=ALL&mode=due")
        self.assertEqual(status, 200)
        self.assertEqual((data["source"], data["cards"]), ("offline", []))

    def test_account_reports_signed_out(self):
        status, data = self.request("GET", "/api/anki/account")
        self.assertEqual(status, 200)
        self.assertEqual((data["signed_in"], data["mode"], data["endpoint_label"]), (False, "offline", "AnkiWeb"))

    def test_login_requires_credentials(self):
        self.assertEqual(self.post("/api/anki/login", {"email": "a@example.com"})[0], 400)

    def test_media_is_served_from_the_headless_collection(self):
        media_dir = self.state.anki_store.media_dir
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "diagram one.png").write_bytes(b"png-bytes")
        self.assertEqual(self.request("GET", "/api/anki/media/diagram%20one.png"), (200, b"png-bytes"))
        self.assertEqual(self.request("GET", "/api/anki/media/missing.png")[0], 404)
        (media_dir / "notes..v2.png").write_bytes(b"dots")
        self.assertEqual(self.request("GET", "/api/anki/media/notes..v2.png"), (200, b"dots"))

    def test_media_rejects_path_traversal(self):
        for path in (
            "/api/anki/media/..",
            "/api/anki/media/../config.json",
            "/api/anki/media/..%2Fconfig.json",
            "/api/anki/media/..%2F..%2Fdata%2Fconfig.json",
            "/api/anki/media/sub%2Ffile.png",
            "/api/anki/media/%2Fetc%2Fpasswd",
            "/api/anki/media/",
        ):
            with self.subTest(path=path):
                self.assertIn(self.request("GET", path)[0], (400, 404))
                self.assertNotEqual(self.request("GET", path)[0], 200)
        for name in ("..", "%2E%2E", ".", "..%2Fconfig.json", "a%00b.png"):
            self.assertEqual(self.request("GET", f"/api/anki/media/{name}")[0], 400, name)

    def test_focus_app_opens_only_valid_app_names(self):
        with mock.patch.object(server.subprocess, "Popen") as popen:
            status, data = self.post("/api/focus-app", {"app": "Orca"})
            self.assertEqual((status, data["app"]), (200, "Orca"))
            popen.assert_called_once_with(["/usr/bin/open", "-a", "Orca"])
            popen.reset_mock()

            for app in ("Orca; rm -rf ~", "$(reboot)", "-a", "../../Applications/X", "a" * 49, "Orca\nTerminal"):
                with self.subTest(app=app):
                    self.assertEqual(self.post("/api/focus-app", {"app": app})[0], 400)
            popen.assert_not_called()

    def test_focus_app_defaults_to_return_app(self):
        self.state.config["return_app"] = "Visual Studio Code"
        with mock.patch.object(server.subprocess, "Popen") as popen:
            self.assertEqual(self.post("/api/focus-app", {})[1]["app"], "Visual Studio Code")
        popen.assert_called_once_with(["/usr/bin/open", "-a", "Visual Studio Code"])

    def test_malformed_requests_get_400(self):
        self.assertEqual(self.post("/api/pause", b"{not json")[0], 400)
        self.assertEqual(self.post("/api/pause", b"[1, 2]")[0], 400)
        self.assertEqual(self.post("/api/pause", {"seconds": "soon"})[0], 400)
        self.assertEqual(self.post("/api/agent/start", {"pid": "abc"})[0], 400)
        self.assertEqual(self.post("/api/anki/sync", {"on_conflict": "merge"})[0], 400)
        self.assertEqual(self.post("/api/nope")[0], 404)

    def test_cross_site_requests_are_rejected(self):
        status, _ = self.post("/api/agent/clear", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.post("/api/focus-app", {"app": "Terminal"}, headers={"Origin": "null"})
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/api/scratchpad", headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(status, 403)

    def test_same_origin_requests_are_allowed(self):
        for origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"):
            with self.subTest(origin=origin):
                self.assertEqual(self.post("/api/agent/clear", headers={"Origin": origin})[0], 200)
        self.assertEqual(self.request("GET", "/api/status", headers={"Host": f"localhost:{self.port}"})[0], 200)


if __name__ == "__main__":
    unittest.main()
