"""End-to-end sync tests against a throwaway `python -m anki.syncserver`."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import support

from ankistore import ANKI_AVAILABLE, AnkiStore, FullSyncConflict, SyncError, is_auth_error

SYNC_PASSWORD = "throwaway-local-password-4711"
SYNC_USERS = [f"test-user-{i}" for i in range(1, 11)]  # one account per test keeps them independent
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
NOTE = dict(
    deck="Agent Focus::Tests",
    notetype="Agent Focus Basic",
    tags=["agent-focus-test"],
    id_tag="af-note-1",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def files_containing(root: Path, needle: bytes) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


@unittest.skipUnless(ANKI_AVAILABLE, "needs the anki package (pip install anki)")
class SyncServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(dir=support.SANDBOX))
        port = free_port()
        env = dict(
            os.environ,
            SYNC_HOST="127.0.0.1",
            SYNC_PORT=str(port),
            SYNC_BASE=str(cls.tmp / "server"),
            **{f"SYNC_USER{i}": f"{user}:{SYNC_PASSWORD}" for i, user in enumerate(SYNC_USERS, 1)},
        )
        cls.free_users = list(SYNC_USERS)
        cls.server_log = (cls.tmp / "server.log").open("wb")
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "anki.syncserver"], env=env, stdout=cls.server_log, stderr=subprocess.STDOUT
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and cls.server.poll() is None:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            cls.tearDownClass()
            raise RuntimeError((cls.tmp / "server.log").read_text())
        cls.endpoint = f"http://127.0.0.1:{port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait()
        cls.server_log.close()

    def setUp(self):
        # ANKI_HEADLESS_HOME outranks the `home` argument; each test wants its own directory.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("ANKI_HEADLESS_HOME", None)
        self.user = self.free_users.pop()

    def store(self, name: str) -> AnkiStore:
        return AnkiStore(home=self.tmp / name, endpoint=self.endpoint, profile="agent-focus")

    def signed_in_store(self, name: str) -> AnkiStore:
        store = self.store(name)
        store.login(self.user, SYNC_PASSWORD)
        return store

    def test_full_round_trip(self):
        # Seed device: sign in, pull the empty server, add a note with media, upload.
        seed = self.store("seed")
        self.assertFalse(seed.is_authenticated())
        seed.login(f"  {self.user} ", SYNC_PASSWORD)
        self.assertTrue(seed.is_authenticated())
        self.assertEqual(stat.S_IMODE(seed.auth_path.stat().st_mode), 0o600)
        saved = json.loads(seed.auth_path.read_text())
        self.assertEqual(set(saved), {"hkey", "endpoint", "account"})
        self.assertEqual((saved["endpoint"], saved["account"]), (self.endpoint, self.user))
        seed.sync(media=False, on_conflict="download")

        with seed.session() as col:
            AnkiStore.ensure_notetype(
                col, NOTE["notetype"], ["Front", "Back"], "{{Front}}", "{{FrontSide}}<hr id=answer>{{Back}}",
                css=".card { color: red; }",
            )
            image = col.media.write_data("agent-focus-test.png", PNG_BYTES)
            fields = {"Front": r"What is \(x^2\)'s derivative?", "Back": f'2x <img src="{image}">'}
            self.assertEqual(AnkiStore.upsert_note(col, fields=fields, **NOTE), "added")
            self.assertEqual(AnkiStore.upsert_note(col, fields=fields, **NOTE), "unchanged")
            fields["Back"] = f'<b>2x</b> <img src="{image}">'
            self.assertEqual(AnkiStore.upsert_note(col, fields=fields, **NOTE), "updated")
            self.assertEqual(AnkiStore.upsert_note(col, fields=fields, **{**NOTE, "tags": ["extra"]}), "updated")
            self.assertEqual(len(col.find_notes(f'tag:{NOTE["id_tag"]}')), 1)
            col.add_note(self.basic_note(col, "Second card", "Answer two"), col.decks.id(NOTE["deck"]))
        self.assertEqual(seed.sync(media=True, on_conflict="upload")["required"], "FULL_UPLOAD")

        # Second device: full download brings notes and media.
        mirror = self.signed_in_store("mirror")
        self.assertEqual(mirror.sync(media=True, on_conflict="download")["required"], "FULL_DOWNLOAD")
        self.assertEqual((mirror.media_dir / image).read_bytes(), PNG_BYTES)
        for store in (seed, mirror):
            self.assertEqual(files_containing(store.home, SYNC_PASSWORD.encode()), [])

        due = mirror.get_cards_payload("ALL", "due")
        self.assertEqual(due["source"], "headless")
        self.assertEqual(due["anki_endpoint"], self.endpoint)
        self.assertEqual((due["total_cards"], due["total_new"], due["filtered_count"]), (2, 2, 2))
        self.assertEqual(due["reviewed_today"], 0)
        self.assertIn(NOTE["deck"], [d["name"] for d in due["decks"]])
        card = next(c for c in due["cards"] if "derivative" in c["front"])
        self.assertNotIn("<style", card["front"])
        self.assertNotIn("derivative", card["back"])
        self.assertIn(f'src="{image}"', card["back"])
        self.assertEqual(len(card["next_reviews"]), 4)
        for label in card["next_reviews"]:
            self.assertTrue(label)
            self.assertNotIn("⁨", label)
            self.assertNotIn("⁩", label)

        deck_only = mirror.get_cards_payload(NOTE["deck"], "all")
        self.assertEqual(deck_only["study_mode"], "all")
        self.assertEqual(deck_only["filtered_count"], 2)
        self.assertEqual(mirror.get_cards_payload("No Such Deck", "all")["filtered_count"], 0)

        # Answer one card with SM-2 and one with FSRS, then sync both ways.
        first, second = (int(c["id"]) for c in due["cards"])
        with mirror.session() as col:
            AnkiStore.answer(col, first, 3)
            self.assertFalse(col.get_card(first).memory_state)
            col.set_config("fsrs", True)
            AnkiStore.answer(col, second, 4)
            self.assertGreater(col.get_card(second).memory_state.stability, 0)
            self.assertEqual(AnkiStore.reviewed_today(col), 2)
        self.assertEqual(mirror.get_cards_payload("ALL", "due")["filtered_count"], 1)
        # A normal sync runs inside sync_collection, which then reports nothing further required.
        self.assertEqual(mirror.sync(media=False)["required"], "NO_CHANGES")
        self.assertEqual(seed.sync(media=False)["required"], "NO_CHANGES")
        with seed.session() as col:
            self.assertEqual(AnkiStore.reviewed_today(col), 2)
            self.assertTrue(col.get_config("fsrs"))

        self.assertEqual(mirror.sync(media=True)["required"], "NO_CHANGES")

    def test_diverged_collections_need_an_explicit_choice(self):
        first = self.signed_in_store("diverge-a")
        first.sync(media=False, on_conflict="download")
        with first.session() as col:
            col.add_note(self.basic_note(col, "From A", "a"), col.decks.id("Default"))
        first.sync(media=False, on_conflict="upload")

        second = self.signed_in_store("diverge-b")
        with second.session() as col:
            col.add_note(self.basic_note(col, "From B", "b"), col.decks.id("Default"))
        with self.assertRaises(FullSyncConflict):
            second.sync(media=False)
        self.assertEqual(second.sync(media=False, on_conflict="download")["required"], "FULL_SYNC")
        with second.session() as col:
            self.assertEqual(len(col.find_notes("From A")), 1)
            self.assertEqual(len(col.find_notes("From B")), 0)

    def test_bad_sync_key_is_an_auth_error(self):
        store = self.store("bad-key")
        store._save_auth("not-a-real-hkey", self.endpoint)
        self.assertTrue(store.is_authenticated())
        with self.assertRaises(SyncError) as caught:
            store.sync(media=False)
        self.assertTrue(is_auth_error(caught.exception))

    def test_wrong_password_is_rejected_and_nothing_is_saved(self):
        store = self.store("wrong-password")
        with self.assertRaises(SyncError) as caught:
            store.login(self.user, "wrong-password")
        self.assertTrue(is_auth_error(caught.exception))
        self.assertFalse(store.auth_path.exists())

    def test_logout_removes_the_sync_key(self):
        store = self.signed_in_store("logout")
        store.logout()
        self.assertFalse(store.auth_path.exists())
        self.assertFalse(store.is_authenticated())
        self.assertFalse(store.auth_info()["signed_in"])
        store.logout()  # signing out twice is fine

    def test_auth_file_is_private_even_if_it_existed_before(self):
        store = self.store("perms")
        store.auth_path.write_text("{}")
        os.chmod(store.auth_path, 0o644)
        store.login(self.user, SYNC_PASSWORD)
        self.assertEqual(stat.S_IMODE(store.auth_path.stat().st_mode), 0o600)

    def test_server_serves_headless_cards_with_media_urls(self):
        import server

        store = self.signed_in_store("server-view")
        store.sync(media=False, on_conflict="download")
        with store.session() as col:
            col.add_note(self.basic_note(col, "Front", 'Back <img src="pic one.png">'), col.decks.id("Default"))
        state = server.FocusState()
        state.anki_store = store
        with mock.patch.object(server, "log"):
            payload = state.get_anki_payload("ALL", "due")
            status, review = state.record_anki_review(payload["cards"][0]["id"], "good")
            state._cancel_sync_timer_locked()
        self.assertEqual(payload["source"], "headless")
        self.assertIn('src="/api/anki/media/pic%20one.png"', payload["cards"][0]["back"])
        self.assertEqual((status, review["source"], review["reviewed_today"]), (200, "headless", 1))
        self.assertTrue(payload["signed_in"])
        self.assertTrue(state._anki_dirty)

    @staticmethod
    def basic_note(col, front: str, back: str):
        note = col.new_note(col.models.by_name("Basic"))
        note["Front"], note["Back"] = front, back
        return note


class OfflineHelpersTest(unittest.TestCase):
    def test_signed_out_store_reports_auth_info(self):
        store = AnkiStore(home=Path(tempfile.mkdtemp(dir=support.SANDBOX)), endpoint="https://sync.example.com/")
        info = store.auth_info()
        self.assertEqual((info["signed_in"], info["endpoint"], info["account"]), (False, "https://sync.example.com/", ""))

    def test_corrupt_auth_file_means_signed_out(self):
        store = AnkiStore(home=Path(tempfile.mkdtemp(dir=support.SANDBOX)))
        store.auth_path.write_text("{oops")
        self.assertFalse(store.is_authenticated())


if __name__ == "__main__":
    unittest.main()
