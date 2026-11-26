"""ankistore.py — shared headless Anki layer (official `anki` package, Rust backend, zero Qt).

Supports both reviewing (FSRS/SM-2 scheduler) and idempotent note creation/upsert.
Uses an isolated local collection + auth.json (chmod 600, hkey + endpoint only; never stores password).
NEVER opens Anki Desktop's User 1/collection.anki2.

    store = AnkiStore(home="~/.local/share/anki-headless", profile="agent-focus")
    store.login(email, password)        # once; persists hkey + endpoint only
    store.sync(on_conflict="download")  # pull collection + media silently
    with store.session() as col:
        card = store.next_card(col)
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    from anki.collection import Collection
    from anki.errors import SyncError
    from anki.scheduler.v3 import CardAnswer
    from anki.sync import SyncAuth
    from anki.sync_pb2 import SyncCollectionResponse as _SCR

    ANKI_AVAILABLE = True
    RATING = {1: CardAnswer.AGAIN, 2: CardAnswer.HARD, 3: CardAnswer.GOOD, 4: CardAnswer.EASY}
except ImportError:  # pragma: no cover - allows stdlib-only fallback
    ANKI_AVAILABLE = False
    Collection = Any  # type: ignore[misc,assignment]
    SyncError = RuntimeError  # type: ignore[misc,assignment]
    SyncAuth = Any  # type: ignore[misc,assignment]
    _SCR = None  # type: ignore[assignment]
    RATING = {}


class FullSyncConflict(RuntimeError):
    """Local and server diverged in schema; caller must pick upload or download explicitly."""


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


def _strip_bidi(text: str) -> str:
    return text.replace("\u2068", "").replace("\u2069", "").strip()


def _split_rendered_answer(answer_html: str) -> str:
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", "", answer_html, flags=re.I | re.S)
    parts = re.split(r"<hr\b[^>]*id=['\"]?answer['\"]?[^>]*>", cleaned, maxsplit=1, flags=re.I)
    return parts[1].strip() if len(parts) == 2 else cleaned.strip()


def _strip_style(html_str: str) -> str:
    return re.sub(r"<style\b[^>]*>.*?</style>", "", html_str, flags=re.I | re.S).strip()


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

    # ---------------------------------------------------------------- auth
    def _save_auth(self, hkey: str, endpoint: str | None, email: str | None = None) -> None:
        payload: dict[str, Any] = {"hkey": hkey, "endpoint": endpoint or None}
        if email:
            payload["account"] = email
        self.auth_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.auth_path, stat.S_IRUSR | stat.S_IWUSR)

    def auth_info(self) -> dict[str, Any]:
        if not self.auth_path.exists():
            return {
                "available": ANKI_AVAILABLE,
                "signed_in": False,
                "endpoint": self.endpoint,
                "account": "",
                "profile": self.profile,
                "has_collection": self.col_path.exists(),
            }
        try:
            d = json.loads(self.auth_path.read_text(encoding="utf-8"))
            signed = bool(d.get("hkey"))
            ep = d.get("endpoint") or self.endpoint or None
            return {
                "available": ANKI_AVAILABLE,
                "signed_in": signed,
                "endpoint": ep,
                "account": str(d.get("account") or ""),
                "profile": self.profile,
                "has_collection": self.col_path.exists(),
            }
        except Exception:
            return {
                "available": ANKI_AVAILABLE,
                "signed_in": False,
                "endpoint": self.endpoint,
                "account": "",
                "profile": self.profile,
                "has_collection": self.col_path.exists(),
            }

    def is_authenticated(self) -> bool:
        return ANKI_AVAILABLE and bool(self.auth_info().get("signed_in"))

    def login(self, user: str, password: str, endpoint: str | None = None) -> SyncAuth:
        """Password is used once and discarded; only hkey (+endpoint) hits disk, chmod 600."""
        if not ANKI_AVAILABLE:
            raise RuntimeError("The 'anki' Python package is not installed.")
        ep = (endpoint if endpoint is not None else self.endpoint) or None
        if isinstance(ep, str):
            ep = ep.strip() or None
        with self.session() as col:
            auth = col.sync_login(username=user.strip(), password=password, endpoint=ep)
        self._save_auth(auth.hkey, auth.endpoint or ep, email=user.strip())
        return auth

    def logout(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.auth_path.unlink()

    def auth(self) -> SyncAuth:
        if not ANKI_AVAILABLE:
            raise RuntimeError("The 'anki' Python package is not installed.")
        d = json.loads(self.auth_path.read_text(encoding="utf-8"))
        a = SyncAuth(hkey=d["hkey"])
        ep = d.get("endpoint") or self.endpoint
        if ep:
            a.endpoint = ep
        return a

    # ---------------------------------------------------------------- session
    @contextlib.contextmanager
    def session(self) -> Iterator[Collection]:
        """Exclusive cross-process lock (a collection is single-writer) + open/close."""
        if not ANKI_AVAILABLE:
            raise RuntimeError("The 'anki' Python package is not installed.")
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            col = Collection(str(self.col_path))
            try:
                yield col
            finally:
                with contextlib.suppress(Exception):
                    col.close()
                fcntl.flock(lf, fcntl.LOCK_UN)

    # ---------------------------------------------------------------- sync
    def sync(self, *, media: bool = True, on_conflict: str = "raise", timeout: float = 600) -> dict[str, Any]:
        """Silent two-way sync. on_conflict: raise | upload | download (only used for FULL_SYNC).

        rslib runs media sync on a background thread; poll media_sync_status().active before closing.
        """
        auth = self.auth()
        saved_account = str(self.auth_info().get("account") or "")
        with self.session() as col:
            out = col.sync_collection(auth, sync_media=media)
            if out.new_endpoint:
                auth.endpoint = out.new_endpoint
                self._save_auth(auth.hkey, auth.endpoint, email=saved_account or None)
            req = out.required
            if req in (_SCR.FULL_SYNC, _SCR.FULL_UPLOAD, _SCR.FULL_DOWNLOAD):
                upload = {
                    _SCR.FULL_UPLOAD: True,
                    _SCR.FULL_DOWNLOAD: False,
                    _SCR.FULL_SYNC: {"upload": True, "download": False}.get(on_conflict),
                }[req]
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
                t0 = time.time()
                while col.media_sync_status().active:
                    if time.time() - t0 > timeout:
                        col.abort_media_sync()
                        raise TimeoutError("Media sync timed out")
                    time.sleep(0.1)
            return {
                "required": _SCR.ChangesRequired.Name(req),
                "server_message": out.server_message,
            }

    # ---------------------------------------------------------------- notes (creation / upsert API)
    @staticmethod
    def ensure_notetype(
        col: Collection,
        name: str,
        fields: Sequence[str],
        qfmt: str,
        afmt: str,
        css: str = "",
        template_name: str = "Karte 1",
    ) -> Any:
        nt = col.models.by_name(name)
        if nt:
            return nt
        nt = col.models.new(name)
        for f in fields:
            col.models.add_field(nt, col.models.new_field(f))
        t = col.models.new_template(template_name)
        t["qfmt"], t["afmt"] = qfmt, afmt
        col.models.add_template(nt, t)
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
        """Idempotent by unique tag `id_tag`. Returns added | updated | unchanged."""
        ids = col.find_notes(f'tag:"{id_tag}"')
        if ids:
            note = col.get_note(ids[0])
            changed = any(note[k] != v for k, v in fields.items())
            for k, v in fields.items():
                note[k] = v
            want = set(tags) | {id_tag}
            if want - set(note.tags):
                note.tags = sorted(set(note.tags) | want)
                changed = True
            if changed:
                col.update_note(note)
            return "updated" if changed else "unchanged"
        did = col.decks.id(deck, create=True)
        note = col.new_note(col.models.by_name(notetype))
        for k, v in fields.items():
            note[k] = v
        note.tags = sorted(set(tags) | {id_tag})
        col.add_note(note, did)
        return "added"

    # ---------------------------------------------------------------- review (Agent Focus API)
    @staticmethod
    def next_card(col: Collection, deck: str | None = None) -> DueCard | None:
        if deck:
            col.decks.select(col.decks.id(deck))
        q = col.sched.get_queued_cards(fetch_limit=1)
        if not q.cards:
            return None
        qc = q.cards[0]
        card = col.get_card(qc.card.id)
        card.start_timer()
        ro = card.render_output(browser=False)
        return DueCard(
            card_id=card.id,
            note_id=card.nid,
            queue={0: "new", 1: "learning", 2: "review"}.get(qc.queue, "review"),
            front_html=_strip_style(ro.question_text),
            back_html=_split_rendered_answer(ro.answer_text),
            css=ro.css,
            buttons=[_strip_bidi(b) for b in col.sched.describe_next_states(qc.states)],
            counts=(q.new_count, q.learning_count, q.review_count),
        )

    @staticmethod
    def answer(col: Collection, card_id: int, rating: int, millis: int = 0) -> None:
        """rating 1..4 = Again/Hard/Good/Easy via the exact rslib scheduler (SM-2 or FSRS)."""
        card = col.get_card(card_id)
        card.start_timer()
        states = col._backend.get_scheduling_states(card_id)
        ans = col.sched.build_answer(card=card, states=states, rating=RATING[rating])
        if millis:
            ans.milliseconds_taken = millis
        col.sched.answer_card(ans)

    @staticmethod
    def reviewed_today(col: Collection) -> int:
        cutoff_ms = int((col.sched.day_cutoff - 86400) * 1000)
        return int(col.db.scalar("SELECT count() FROM revlog WHERE id > ?", cutoff_ms) or 0)

    @staticmethod
    def deck_stats(col: Collection) -> list[dict[str, Any]]:
        """Flatten deck_due_tree() into sorted per-deck stats matching Agent Focus UI schema."""
        tree = col.sched.deck_due_tree()
        decks_out: list[dict[str, Any]] = []

        def walk(node: Any, prefix: str = "") -> None:
            for ch in node.children:
                full_name = f"{prefix}::{ch.name}" if prefix else ch.name
                total = int(getattr(ch, "total_in_deck", 0))
                if full_name == "Default" and total == 0 and not ch.children:
                    continue
                decks_out.append(
                    {
                        "name": full_name,
                        "label": full_name.replace("::", " / "),
                        "count": total,
                        "due": int(ch.review_count) + int(ch.learn_count),
                        "new": int(ch.new_count),
                    }
                )
                if ch.children:
                    walk(ch, full_name)

        walk(tree)
        decks_out.sort(key=lambda d: d["name"].lower())
        return decks_out

    def get_cards_payload(
        self,
        deck_filter: str = "ALL",
        study_mode: str = "due",
        limit: int = 80,
    ) -> dict[str, Any]:
        """Build the full cards + decks + counters payload for Agent Focus."""
        with self.session() as col:
            decks_out = self.deck_stats(col)
            reviewed_today = self.reviewed_today(col)

            all_ids = list(col.find_cards("-is:suspended"))
            all_due_ids = list(col.find_cards("(is:due OR is:learn) -is:suspended"))
            all_new_ids = list(col.find_cards("is:new -is:suspended"))

            deck_q = ""
            if deck_filter and deck_filter != "ALL":
                safe_deck = deck_filter.replace('"', '\\"')
                deck_q = f'deck:"{safe_deck}" '

            if study_mode == "all":
                target_ids = list(col.find_cards(f"{deck_q}-is:suspended"))
            else:
                due_ids = list(col.find_cards(f"{deck_q}(is:due OR is:learn) -is:suspended"))
                new_ids = list(col.find_cards(f"{deck_q}is:new -is:suspended"))
                target_ids = due_ids + new_ids

            selected_ids = target_ids[:limit]
            cards_out: list[dict[str, Any]] = []
            for cid in selected_ids:
                card = col.get_card(cid)
                deck_raw = col.decks.name(card.did)
                nt = card.note_type()
                nt_name = nt["name"] if nt else "Basic"
                ro = card.render_output(browser=False)
                front_html = _strip_style(ro.question_text)
                back_html = _split_rendered_answer(ro.answer_text)
                try:
                    states = col._backend.get_scheduling_states(cid)
                    next_reviews = [_strip_bidi(b) for b in col.sched.describe_next_states(states)]
                except Exception:
                    next_reviews = []

                cards_out.append(
                    {
                        "id": str(card.id),
                        "nid": str(card.nid),
                        "deck": deck_raw.replace("::", " / "),
                        "deck_raw": deck_raw,
                        "notetype": nt_name,
                        "queue": int(card.queue),
                        "ivl": int(card.ivl),
                        "reps": int(card.reps),
                        "front": front_html,
                        "back": back_html,
                        "next_reviews": next_reviews,
                    }
                )

            info = self.auth_info()
            return {
                "source": "headless",
                "ankiconnect": True,
                "anki_mode": "headless",
                "anki_endpoint": info.get("endpoint") or "AnkiWeb",
                "anki_account": info.get("account") or "",
                "study_mode": study_mode,
                "decks": decks_out,
                "total_cards": len(all_ids),
                "total_due": len(all_due_ids),
                "total_new": len(all_new_ids),
                "filtered_count": len(cards_out),
                "reviewed_today": reviewed_today,
                "cards": cards_out,
            }
