"""Headless Anki collection for Agent Focus, built on the official `anki` package (no Qt).

Keeps its own collection and auth.json under `<home>/<profile>/`, never Anki Desktop's.
auth.json holds the sync key and endpoint, never the password, and is created 0600.

    store = AnkiStore(home="~/.local/share/anki-headless", profile="agent-focus")
    store.login(email, password)
    store.sync(on_conflict="download")
    with store.session() as col:
        card = store.next_card(col)
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    from anki.collection import Collection
    from anki.errors import SyncError, SyncErrorKind
    from anki.scheduler.v3 import CardAnswer
    from anki.sync import SyncAuth
    from anki.sync_pb2 import SyncCollectionResponse as _SCR

    ANKI_AVAILABLE = True
    RATING = {1: CardAnswer.AGAIN, 2: CardAnswer.HARD, 3: CardAnswer.GOOD, 4: CardAnswer.EASY}
except ImportError:  # the server still runs on stdlib Python, with AnkiConnect or the offline snapshot
    ANKI_AVAILABLE = False
    Collection = Any  # type: ignore[misc,assignment]
    SyncError = RuntimeError  # type: ignore[misc,assignment]
    SyncAuth = Any  # type: ignore[misc,assignment]
    RATING = {}

FULL_SYNC_KINDS = ("FULL_SYNC", "FULL_UPLOAD", "FULL_DOWNLOAD")


class FullSyncConflict(RuntimeError):
    """Local and server collections diverged; the caller must pick upload or download."""


@dataclass
class DueCard:
    card_id: int
    note_id: int
    queue: str  # new | learning | review
    front_html: str
    back_html: str
    css: str
    buttons: list[str]
    counts: tuple[int, int, int]  # (new, learning, review)


def is_auth_error(exc: BaseException) -> bool:
    return ANKI_AVAILABLE and isinstance(exc, SyncError) and exc.kind == SyncErrorKind.AUTH


def strip_bidi(text: str) -> str:
    """Anki wraps interval labels like "10m" in Unicode isolates; the UI wants plain text."""
    return text.replace("⁨", "").replace("⁩", "").strip()


def strip_style(markup: str) -> str:
    return re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", markup, flags=re.I | re.S).strip()


def split_rendered_answer(answer_html: str) -> str:
    """Return only the back side of a rendered answer (the part after <hr id=answer>)."""
    cleaned = strip_style(answer_html)
    parts = re.split(r"<hr\b[^>]*id=['\"]?answer['\"]?[^>]*>", cleaned, maxsplit=1, flags=re.I)
    return parts[1].strip() if len(parts) == 2 else cleaned


def _require_anki() -> None:
    if not ANKI_AVAILABLE:
        raise RuntimeError("The 'anki' Python package is not installed.")


class AnkiStore:
    def __init__(
        self,
        home: str | os.PathLike[str] = "~/.local/share/anki-headless",
        endpoint: str | None = None,
        profile: str = "agent-focus",
    ):
        base = os.environ.get("ANKI_HEADLESS_HOME") or str(home)
        self.profile = profile
        self.home = Path(base).expanduser() / profile
        self.home.mkdir(parents=True, exist_ok=True)
        self.col_path = self.home / "collection.anki2"
        self.media_dir = self.home / "collection.media"
        self.auth_path = self.home / "auth.json"
        self.lock_path = self.home / ".lock"
        self.endpoint = (endpoint or os.environ.get("ANKI_SYNC_ENDPOINT") or "").strip() or None

    # auth

    def _read_auth(self) -> dict[str, Any]:
        try:
            data = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_auth(self, hkey: str, endpoint: str | None, account: str = "") -> None:
        payload: dict[str, Any] = {"hkey": hkey, "endpoint": endpoint or None}
        if account:
            payload["account"] = account
        fd = os.open(self.auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, indent=2) + "\n")
        os.chmod(self.auth_path, 0o600)  # O_CREAT's mode is ignored when the file already exists

    def auth_info(self) -> dict[str, Any]:
        saved = self._read_auth()
        return {
            "available": ANKI_AVAILABLE,
            "signed_in": bool(saved.get("hkey")),
            "endpoint": saved.get("endpoint") or self.endpoint,
            "account": str(saved.get("account") or ""),
            "profile": self.profile,
            "has_collection": self.col_path.exists(),
        }

    def is_authenticated(self) -> bool:
        return ANKI_AVAILABLE and bool(self._read_auth().get("hkey"))

    def login(self, user: str, password: str, endpoint: str | None = None) -> SyncAuth:
        """Exchange the password for a sync key. Only the key and endpoint are written to disk."""
        _require_anki()
        ep = ((self.endpoint if endpoint is None else endpoint) or "").strip() or None
        user = user.strip()
        with self.session() as col:
            auth = col.sync_login(username=user, password=password, endpoint=ep)
        self._save_auth(auth.hkey, auth.endpoint or ep, account=user)
        return auth

    def logout(self) -> None:
        self.auth_path.unlink(missing_ok=True)

    def auth(self) -> SyncAuth:
        _require_anki()
        saved = self._read_auth()
        auth = SyncAuth(hkey=saved["hkey"])
        endpoint = saved.get("endpoint") or self.endpoint
        if endpoint:
            auth.endpoint = endpoint
        return auth

    # session

    @contextlib.contextmanager
    def session(self) -> Iterator[Collection]:
        """Open the collection under an exclusive cross-process lock (collections are single-writer)."""
        _require_anki()
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            col = Collection(str(self.col_path))
            try:
                yield col
            finally:
                # close() raises when a failed full sync left the collection closed;
                # don't let that mask the original error.
                with contextlib.suppress(Exception):
                    col.close()

    # sync

    def sync(self, *, media: bool = True, on_conflict: str = "raise", timeout: float = 600) -> dict[str, Any]:
        """Two-way sync with no GUI. on_conflict (raise | upload | download) only applies to FULL_SYNC."""
        auth = self.auth()
        account = str(self._read_auth().get("account") or "")
        with self.session() as col:
            out = col.sync_collection(auth, sync_media=media)
            if out.new_endpoint:
                auth.endpoint = out.new_endpoint
                self._save_auth(auth.hkey, auth.endpoint, account=account)
            required = _SCR.ChangesRequired.Name(out.required)
            if required in FULL_SYNC_KINDS:
                upload = {
                    "FULL_UPLOAD": True,
                    "FULL_DOWNLOAD": False,
                    "FULL_SYNC": {"upload": True, "download": False}.get(on_conflict),
                }[required]
                if upload is None:
                    raise FullSyncConflict("Collection diverged; choose download or upload explicitly.")
                col.close_for_full_sync()
                col.full_upload_or_download(
                    auth=auth,
                    server_usn=out.server_media_usn if media else None,
                    upload=upload,
                )
                col.reopen(after_full_sync=True)
            elif media:
                col.sync_media(auth)
            if media:
                # rslib syncs media on a background thread; wait for it before closing.
                started = time.time()
                while col.media_sync_status().active:
                    if time.time() - started > timeout:
                        col.abort_media_sync()
                        raise TimeoutError("Media sync timed out")
                    time.sleep(0.1)
            return {"required": required, "server_message": out.server_message}

    # notes

    @staticmethod
    def ensure_notetype(
        col: Collection,
        name: str,
        fields: Sequence[str],
        qfmt: str,
        afmt: str,
        css: str = "",
        template_name: str = "Card 1",
    ) -> Any:
        existing = col.models.by_name(name)
        if existing:
            return existing
        nt = col.models.new(name)
        for field in fields:
            col.models.add_field(nt, col.models.new_field(field))
        template = col.models.new_template(template_name)
        template["qfmt"], template["afmt"] = qfmt, afmt
        col.models.add_template(nt, template)
        nt["css"] = css
        col.models.add(nt)
        return col.models.by_name(name)

    @staticmethod
    def upsert_note(
        col: Collection,
        *,
        deck: str,
        notetype: str,
        fields: dict[str, str],
        tags: Sequence[str],
        id_tag: str,
    ) -> str:
        """Create or update the note tagged `id_tag`. Returns added | updated | unchanged."""
        wanted_tags = set(tags) | {id_tag}
        ids = col.find_notes(f'tag:"{id_tag}"')
        if not ids:
            note = col.new_note(col.models.by_name(notetype))
            for key, value in fields.items():
                note[key] = value
            note.tags = sorted(wanted_tags)
            col.add_note(note, col.decks.id(deck, create=True))
            return "added"
        note = col.get_note(ids[0])
        changed = any(note[key] != value for key, value in fields.items())
        for key, value in fields.items():
            note[key] = value
        if wanted_tags - set(note.tags):
            note.tags = sorted(set(note.tags) | wanted_tags)
            changed = True
        if changed:
            col.update_note(note)
        return "updated" if changed else "unchanged"

    # review

    @staticmethod
    def next_card(col: Collection, deck: str | None = None) -> DueCard | None:
        if deck:
            col.decks.select(col.decks.id(deck))
        queued = col.sched.get_queued_cards(fetch_limit=1)
        if not queued.cards:
            return None
        top = queued.cards[0]
        card = col.get_card(top.card.id)
        card.start_timer()
        rendered = card.render_output(browser=False)
        return DueCard(
            card_id=card.id,
            note_id=card.nid,
            queue={0: "new", 1: "learning", 2: "review"}.get(top.queue, "review"),
            front_html=strip_style(rendered.question_text),
            back_html=split_rendered_answer(rendered.answer_text),
            css=rendered.css,
            buttons=[strip_bidi(b) for b in col.sched.describe_next_states(top.states)],
            counts=(queued.new_count, queued.learning_count, queued.review_count),
        )

    @staticmethod
    def answer(col: Collection, card_id: int, rating: int, millis: int = 0) -> None:
        """Rate 1..4 (Again/Hard/Good/Easy) with the collection's own scheduler, SM-2 or FSRS."""
        card = col.get_card(card_id)
        card.start_timer()
        states = col._backend.get_scheduling_states(card_id)
        answer = col.sched.build_answer(card=card, states=states, rating=RATING[rating])
        if millis:
            answer.milliseconds_taken = millis
        col.sched.answer_card(answer)

    @staticmethod
    def reviewed_today(col: Collection) -> int:
        cutoff_ms = int((col.sched.day_cutoff - 86400) * 1000)
        return int(col.db.scalar("SELECT count() FROM revlog WHERE id > ?", cutoff_ms) or 0)

    @staticmethod
    def deck_stats(col: Collection) -> list[dict[str, Any]]:
        decks: list[dict[str, Any]] = []

        def walk(node: Any, prefix: str = "") -> None:
            for child in node.children:
                name = f"{prefix}::{child.name}" if prefix else child.name
                total = int(child.total_in_deck)
                if name == "Default" and total == 0 and not child.children:
                    continue
                decks.append(
                    {
                        "name": name,
                        "label": name.replace("::", " / "),
                        "count": total,
                        "due": int(child.review_count) + int(child.learn_count),
                        "new": int(child.new_count),
                    }
                )
                walk(child, name)

        walk(col.sched.deck_due_tree())
        decks.sort(key=lambda d: d["name"].lower())
        return decks

    def get_cards_payload(self, deck_filter: str = "ALL", study_mode: str = "due", limit: int = 80) -> dict[str, Any]:
        with self.session() as col:
            deck_q = ""
            if deck_filter and deck_filter != "ALL":
                deck_q = 'deck:"{}" '.format(deck_filter.replace('"', '\\"'))
            if study_mode == "all":
                target_ids = list(col.find_cards(f"{deck_q}-is:suspended"))
            else:
                target_ids = list(col.find_cards(f"{deck_q}(is:due OR is:learn) -is:suspended"))
                target_ids += col.find_cards(f"{deck_q}is:new -is:suspended")

            cards = []
            for cid in target_ids[:limit]:
                card = col.get_card(cid)
                deck_raw = col.decks.name(card.did)
                rendered = card.render_output(browser=False)
                states = col._backend.get_scheduling_states(cid)
                cards.append(
                    {
                        "id": str(card.id),
                        "nid": str(card.nid),
                        "deck": deck_raw.replace("::", " / "),
                        "deck_raw": deck_raw,
                        "notetype": card.note_type()["name"],
                        "queue": int(card.queue),
                        "ivl": int(card.ivl),
                        "reps": int(card.reps),
                        "front": strip_style(rendered.question_text),
                        "back": split_rendered_answer(rendered.answer_text),
                        "next_reviews": [strip_bidi(b) for b in col.sched.describe_next_states(states)],
                    }
                )

            return {
                "source": "headless",
                "anki_endpoint": self.auth_info()["endpoint"] or "AnkiWeb",
                "study_mode": study_mode,
                "decks": self.deck_stats(col),
                "total_cards": len(col.find_cards("-is:suspended")),
                "total_due": len(col.find_cards("(is:due OR is:learn) -is:suspended")),
                "total_new": len(col.find_cards("is:new -is:suspended")),
                "filtered_count": len(cards),
                "reviewed_today": self.reviewed_today(col),
                "cards": cards,
            }
