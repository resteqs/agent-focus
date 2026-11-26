#!/usr/bin/env python3
"""Agent Focus daemon.

While a coding agent (Claude Code, Codex, Antigravity) is working, polls Safari's active
tabs over AppleScript and redirects blocked sites to the wait-state page served on 127.0.0.1.
"""

from __future__ import annotations

import html
import json
import math
import mimetypes
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from ankistore import (
    ANKI_AVAILABLE,
    AnkiStore,
    FullSyncConflict,
    SyncError,
    is_auth_error,
    split_rendered_answer,
    strip_style,
)

HOST = "127.0.0.1"
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(
    os.environ.get("AGENT_FOCUS_DATA_DIR") or (Path.home() / ".local/share/agent-focus")
).expanduser()
CONFIG_PATH = DATA_DIR / "config.json"
SCRATCHPAD_PATH = DATA_DIR / "scratchpad.md"
ANKI_PROGRESS_PATH = DATA_DIR / "anki_progress.json"
ANKICONNECT_URL = os.environ.get("AGENT_FOCUS_ANKICONNECT_URL", "http://127.0.0.1:8765")
MAX_BODY_BYTES = 1024 * 1024
HEADLESS_SYNC_DEBOUNCE_SECONDS = 65.0

EASES = ("again", "hard", "good", "easy")
STUDY_XP = {"again": 0, "hard": 10, "good": 15, "easy": 20}
COMBO_BONUS_XP = 5  # per rating once the combo reaches 3
APP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}")

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 7842,
    "poll_interval_ms": 450,
    "session_ttl_seconds": 1500,
    "default_bypass_seconds": 120,
    "bypass_cooldown_seconds": 30,
    "auto_restore": False,
    "return_app": "Orca",
    "completion_chime": True,
    "obsidian_note_path": "",
    "anki_profile_dir": str(Path.home() / "Library/Application Support/Anki2/User 1"),
    "anki_headless_home": str(Path.home() / ".local/share/anki-headless"),
    "anki_headless_profile": "agent-focus",
    "anki_sync_endpoint": "",
    "blocked_domains": [
        "mail.proton.me",
        "reddit.com",
        "linkedin.com",
        "news.ycombinator.com",
        "x.com",
        "twitter.com",
        "instagram.com",
        "youtube.com",
        "nytimes.com",
        "bbc.com",
        "theguardian.com",
        "tagesschau.de",
        "spiegel.de",
    ],
}


class BadRequest(ValueError):
    pass


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sys.stderr.write(f"[{ts}] {msg}\n")
    sys.stderr.flush()


def today() -> str:
    return date.today().isoformat()


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def normalize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    if "://" in raw:
        raw = urllib.parse.urlparse(raw).hostname or ""
    else:
        raw = raw.split("/")[0].split(":")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw.strip(".")


def normalize_domains(raw_domains: Iterable[Any]) -> list[str]:
    return [d for d in dict.fromkeys(normalize_domain(str(d)) for d in raw_domains) if d]


def host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def int_field(body: dict[str, Any], key: str, default: int = 0) -> int:
    value = body.get(key)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be an integer") from None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


def ankiconnect(action: str, params: dict[str, Any] | None = None, timeout: float = 2.0) -> Any:
    payload = json.dumps({"action": action, "version": 6, "params": params or {}}).encode("utf-8")
    req = urllib.request.Request(
        ANKICONNECT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


def ankiconnect_alive(timeout: float) -> bool:
    try:
        return bool(ankiconnect("version", timeout=timeout))
    except (OSError, ValueError, RuntimeError):
        return False


def rewrite_media_urls(markup: str) -> str:
    """Point relative <img src> at /api/anki/media/ so the page can load collection media."""

    def repl(match: re.Match[str]) -> str:
        quote, src = match.group(1), match.group(2)
        if src.startswith(("http://", "https://", "data:", "/api/")):
            return match.group(0)
        return f"src={quote}/api/anki/media/{urllib.parse.quote(os.path.basename(src))}{quote}"

    return re.sub(r'src=(["\'])([^"\']+)\1', repl, markup)


def render_cloze(text: str, card_ord: int, reveal: bool) -> str:
    def repl(match: re.Match[str]) -> str:
        idx, answer, hint = int(match.group(1)), match.group(2), match.group(3)
        if idx != card_ord + 1:
            return answer
        if reveal:
            return f'<span class="cloze-reveal">{answer}</span>'
        return f'<span class="cloze-blank">[{html.escape(hint) if hint else "…"}]</span>'

    return re.sub(r"\{\{c(\d+)::([^:}]+)(?:::([^}]+))?\}\}", repl, text)


class FocusState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.config: dict[str, Any] = self._load_config()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.bypasses: dict[str, float] = {}
        self.intercept_started: dict[str, float] = {}
        self.paused_until = 0.0
        self.manual_focus = False
        self.last_intercept: dict[str, Any] | None = None
        self.intercept_count_today = 0
        self.intercept_day = today()
        self.sse_clients: list[queue.Queue[str]] = []
        self.anki_cache: dict[str, Any] = {"mtime": 0.0, "decks": [], "cards": []}
        self.anki_progress = self._load_anki_progress()
        self._anki_sync_timer: threading.Timer | None = None
        self._anki_dirty = False
        self.anki_store = AnkiStore(
            home=str(self.config.get("anki_headless_home") or DEFAULT_CONFIG["anki_headless_home"]),
            endpoint=str(self.config.get("anki_sync_endpoint") or "").strip() or None,
            profile=str(self.config.get("anki_headless_profile") or DEFAULT_CONFIG["anki_headless_profile"]),
        )

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except ValueError as e:
                log(f"Ignoring unreadable config {CONFIG_PATH}: {e}")
            else:
                if isinstance(loaded, dict):
                    cfg.update(loaded)
        cfg["blocked_domains"] = normalize_domains(cfg.get("blocked_domains", []))
        return cfg

    def save_config(self) -> None:
        with self.lock:
            write_json_atomic(CONFIG_PATH, self.config)

    def _load_anki_progress(self) -> dict[str, Any]:
        progress: dict[str, Any] = {
            "day": today(),
            "reviewed_today": 0,
            "xp_today": 0,
            "combo": 0,
            "streak_days": 0,
            "last_study_day": "",
            "cards": {},
        }
        if ANKI_PROGRESS_PATH.exists():
            try:
                loaded = json.loads(ANKI_PROGRESS_PATH.read_text(encoding="utf-8"))
            except ValueError as e:
                log(f"Ignoring unreadable study progress: {e}")
            else:
                if isinstance(loaded, dict):
                    progress.update(loaded)
        return progress

    def _save_anki_progress(self) -> None:
        write_json_atomic(ANKI_PROGRESS_PATH, self.anki_progress)

    def broadcast(self, event_name: str = "state") -> None:
        msg = f"event: {event_name}\ndata: {json.dumps(self.snapshot())}\n\n"
        with self.lock:
            for q in list(self.sse_clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    self.sse_clients.remove(q)

    def _roll_intercept_day_locked(self) -> bool:
        if self.intercept_day == today():
            return False
        self.intercept_day = today()
        self.intercept_count_today = 0
        return True

    def prune_locked(self) -> bool:
        now = time.time()
        ttl = float(self.config.get("session_ttl_seconds", 1500))
        was_active = bool(self.sessions) or self.manual_focus

        stale = [
            sid
            for sid, info in self.sessions.items()
            if now - info["updated_at"] > ttl or (info["pid"] > 1 and not pid_alive(info["pid"]))
        ]
        expired = [d for d, until in self.bypasses.items() if until <= now]
        for sid in stale:
            del self.sessions[sid]
        for d in expired:
            del self.bypasses[d]
        changed = bool(stale or expired)

        if self.paused_until and self.paused_until <= now:
            self.paused_until = 0.0
            changed = True

        if was_active and not (self.sessions or self.manual_focus):
            if self.intercept_started:
                self.intercept_started.clear()
                changed = True
            if self._anki_dirty and self.anki_store.is_authenticated():
                threading.Thread(
                    target=self.flush_anki_sync,
                    kwargs={"only_if_dirty": True, "media": False},
                    daemon=True,
                ).start()

        return self._roll_intercept_day_locked() or changed

    def start_session(self, session_id: str, agent: str, pid: int = 0, cwd: str = "", prompt: str = "") -> dict[str, Any]:
        now = time.time()
        with self.lock:
            self.prune_locked()
            session = self.sessions.setdefault(
                session_id,
                {
                    "session_id": session_id,
                    "agent": agent or "agent",
                    "pid": 0,
                    "cwd": "",
                    "project": "",
                    "prompt": "",
                    "started_at": now,
                },
            )
            session["updated_at"] = now
            if pid > 1:
                session["pid"] = pid
            if cwd:
                session["cwd"] = cwd
                session["project"] = Path(cwd).name
            if prompt:
                session["prompt"] = prompt[:160]
        self.broadcast("state")
        return self.snapshot()

    def stop_session(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            self.sessions.pop(session_id, None)
            self.prune_locked()
        self.broadcast("state")
        return self.snapshot()

    def clear_sessions(self) -> dict[str, Any]:
        with self.lock:
            self.sessions.clear()
            self.manual_focus = False
            self.intercept_started.clear()
        self.broadcast("state")
        return self.snapshot()

    def set_manual_focus(self, enabled: bool) -> dict[str, Any]:
        with self.lock:
            self.manual_focus = enabled
            if enabled:
                self.paused_until = 0.0
            elif not self.sessions:
                self.intercept_started.clear()
        self.broadcast("state")
        return self.snapshot()

    def set_pause(self, seconds: int) -> dict[str, Any]:
        with self.lock:
            self.paused_until = time.time() + seconds if seconds > 0 else 0.0
        self.broadcast("state")
        return self.snapshot()

    def _cooldown_left(self, started: float, now: float) -> int:
        cooldown = int(self.config.get("bypass_cooldown_seconds", 30))
        return max(0, math.ceil(cooldown - (now - started)))

    def add_bypass(self, domain: str, seconds: int) -> int:
        """Open `domain` for `seconds` once its cooldown has passed. Returns seconds still locked."""
        nd = normalize_domain(domain)
        now = time.time()
        with self.lock:
            remaining = self._cooldown_left(self.intercept_started.setdefault(nd, now), now)
            if remaining:
                return remaining
            self.bypasses[nd] = now + max(10, seconds)
            del self.intercept_started[nd]
        self.broadcast("state")
        return 0

    def clear_bypass(self, domain: str) -> dict[str, Any]:
        with self.lock:
            self.bypasses.pop(normalize_domain(domain), None)
        self.broadcast("state")
        return self.snapshot()

    def is_blocking_active(self) -> bool:
        with self.lock:
            self.prune_locked()
            return self.paused_until <= time.time() and (bool(self.sessions) or self.manual_focus)

    def match_blocked_domain(self, url: str) -> str | None:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host in ("", "localhost", "127.0.0.1", "::1"):
            return None
        host = host.removeprefix("www.")
        with self.lock:
            now = time.time()
            if any(until > now and host_matches(host, d) for d, until in self.bypasses.items()):
                return None
            return next((d for d in self.config["blocked_domains"] if host_matches(host, d)), None)

    def record_intercept(self, domain: str, url: str) -> float:
        nd = normalize_domain(domain)
        now = time.time()
        with self.lock:
            self._roll_intercept_day_locked()
            self.intercept_count_today += 1
            started = self.intercept_started.setdefault(nd, now)
            self.last_intercept = {"domain": nd, "url": url, "at": now}
        self.broadcast("intercept")
        return started

    def _obsidian_label(self) -> str:
        raw = str(self.config.get("obsidian_note_path") or "").strip()
        if not raw:
            return ""
        p = Path(raw).expanduser()
        return f"{p.parent.name}/{p.name}" if p.parent.name else p.name

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self.prune_locked()
            now = time.time()
            agents = sorted(self.sessions.values(), key=lambda s: s["started_at"])
            paused_remaining = max(0, int(self.paused_until - now))
            return {
                "now": now,
                "blocking": (bool(agents) or self.manual_focus) and paused_remaining == 0,
                "manual_focus": self.manual_focus,
                "paused_remaining": paused_remaining,
                "active_count": len(agents),
                "agents": agents,
                "earliest_start": agents[0]["started_at"] if agents else None,
                "bypasses": {d: int(until - now) for d, until in self.bypasses.items() if until > now},
                "bypass_cooldowns": {d: self._cooldown_left(ts, now) for d, ts in self.intercept_started.items()},
                "intercept_count_today": self.intercept_count_today,
                "last_intercept": self.last_intercept,
                "config": {
                    "auto_restore": bool(self.config.get("auto_restore")),
                    "return_app": str(self.config.get("return_app") or "Orca"),
                    "completion_chime": bool(self.config.get("completion_chime", True)),
                    "default_bypass_seconds": int(self.config.get("default_bypass_seconds", 120)),
                    "bypass_cooldown_seconds": int(self.config.get("bypass_cooldown_seconds", 30)),
                    "blocked_domains": list(self.config["blocked_domains"]),
                    "obsidian_configured": bool(self._obsidian_label()),
                    "obsidian_label": self._obsidian_label(),
                    "anki_headless_available": ANKI_AVAILABLE,
                    "anki_headless_signed_in": self.anki_store.is_authenticated(),
                },
            }

    # Anki: headless store first, then AnkiConnect, then a read-only copy of Anki Desktop's collection.

    def _anki_profile_dir(self) -> Path:
        return Path(self.config["anki_profile_dir"]).expanduser()

    def anki_account_status(self) -> dict[str, Any]:
        info = self.anki_store.auth_info()
        if info["signed_in"]:
            mode = "headless"
        elif ankiconnect_alive(timeout=0.35):
            mode = "ankiconnect"
        else:
            mode = "offline"
        return {**info, "mode": mode, "endpoint_label": info["endpoint"] or "AnkiWeb"}

    def login_anki(self, email: str, password: str, endpoint: str | None = None) -> dict[str, Any]:
        self.anki_store.login(email, password, endpoint=endpoint)
        # Pull before any local write so a fresh cache can't cause a FULL_SYNC conflict.
        sync_res = self.anki_store.sync(media=True, on_conflict="download")
        with self.lock:
            self._anki_dirty = False
            self.config["anki_sync_endpoint"] = endpoint or ""
            self.save_config()
        self.broadcast("state")
        return {"ok": True, "account": self.anki_account_status(), "sync": sync_res}

    def _cancel_sync_timer_locked(self) -> None:
        if self._anki_sync_timer:
            self._anki_sync_timer.cancel()
            self._anki_sync_timer = None

    def logout_anki(self) -> dict[str, Any]:
        with self.lock:
            self._cancel_sync_timer_locked()
            self._anki_dirty = False
        self.anki_store.logout()
        self.broadcast("state")
        return {"ok": True, "account": self.anki_account_status()}

    def flush_anki_sync(self, only_if_dirty: bool = True, media: bool = True, on_conflict: str = "raise") -> tuple[bool, dict[str, Any]]:
        with self.lock:
            if only_if_dirty and not self._anki_dirty:
                return True, {"skipped": True}
            self._cancel_sync_timer_locked()
            self._anki_dirty = False

        if self.anki_store.is_authenticated():
            try:
                res = self.anki_store.sync(media=media, on_conflict=on_conflict)
            except Exception as e:  # network, auth, and rslib errors all end up here
                with self.lock:
                    self._anki_dirty = True
                log(f"Headless Anki sync failed: {e}")
                return False, {
                    "mode": "headless",
                    "error": str(e),
                    "conflict": isinstance(e, FullSyncConflict),
                    "auth_error": is_auth_error(e),
                }
            log(f"Headless Anki sync complete: {res['required']}")
            return True, {"mode": "headless", **res}

        # AnkiConnect's `sync` calls mw.onSync(), which pops a "Sync complete" dialog in
        # Anki Desktop. Only run it when the user asked for a sync, never in the background.
        if only_if_dirty:
            return True, {"skipped": True}
        try:
            ankiconnect("sync", timeout=10.0)
        except (OSError, ValueError, RuntimeError) as e:
            log(f"AnkiConnect sync failed: {e}")
            return False, {"error": str(e), "mode": "ankiconnect"}
        return True, {"mode": "ankiconnect"}

    def _schedule_headless_sync(self) -> None:
        with self.lock:
            self._anki_dirty = True
            self._cancel_sync_timer_locked()
            timer = threading.Timer(
                HEADLESS_SYNC_DEBOUNCE_SECONDS,
                self.flush_anki_sync,
                kwargs={"only_if_dirty": True, "media": False},
            )
            timer.daemon = True
            timer.start()
            self._anki_sync_timer = timer

    def _roll_study_day_locked(self) -> None:
        if self.anki_progress["day"] != today():
            self.anki_progress.update(day=today(), reviewed_today=0, xp_today=0, combo=0)
            self._save_anki_progress()

    def _study_stats_locked(self) -> dict[str, int]:
        self._roll_study_day_locked()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        streak_alive = self.anki_progress["last_study_day"] in (today(), yesterday)
        return {
            "xp_today": int(self.anki_progress["xp_today"]),
            "combo": int(self.anki_progress["combo"]),
            "streak_days": int(self.anki_progress["streak_days"]) if streak_alive else 0,
        }

    def _award_study_xp_locked(self, ease: str) -> dict[str, int]:
        self._roll_study_day_locked()
        progress = self.anki_progress
        if progress["last_study_day"] != today():
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            progress["streak_days"] = int(progress["streak_days"]) + 1 if progress["last_study_day"] == yesterday else 1
            progress["last_study_day"] = today()

        gained = 0
        if ease == "again":
            progress["combo"] = 0
        else:
            progress["combo"] = int(progress["combo"]) + 1
            gained = STUDY_XP[ease] + (COMBO_BONUS_XP if progress["combo"] >= 3 else 0)
            progress["xp_today"] = int(progress["xp_today"]) + gained
        self._save_anki_progress()
        return {"gained_xp": gained, **self._study_stats_locked()}

    def _get_anki_via_connect(self, deck_filter: str, study_mode: str) -> dict[str, Any] | None:
        if not ankiconnect_alive(timeout=0.6):
            return None
        try:
            deck_names: list[str] = ankiconnect("deckNames") or []
            deck_stats: dict[str, Any] = ankiconnect("getDeckStats", {"decks": deck_names}) or {}
            stats_by_name = {v.get("name"): v for v in deck_stats.values()}
            decks = []
            for name in sorted(deck_names):
                st = stats_by_name.get(name, {})
                total = int(st.get("total_in_deck", 0))
                if total == 0 and name == "Default" and len(deck_names) > 1:
                    continue
                decks.append(
                    {
                        "name": name,
                        "label": name.replace("::", " / "),
                        "count": total,
                        "due": int(st.get("review_count", 0)) + int(st.get("learn_count", 0)),
                        "new": int(st.get("new_count", 0)),
                    }
                )

            def find(query: str) -> list[int]:
                return ankiconnect("findCards", {"query": query}) or []

            deck_q = ""
            if deck_filter and deck_filter != "ALL":
                deck_q = 'deck:"{}" '.format(deck_filter.replace('"', '\\"'))
            if study_mode == "all":
                card_ids = find(f"{deck_q}-is:suspended")
            else:
                card_ids = list(dict.fromkeys(find(f"{deck_q}(is:due OR is:learn) -is:suspended") + find(f"{deck_q}is:new -is:suspended")))
            card_ids = card_ids[:100]
            infos = ankiconnect("cardsInfo", {"cards": card_ids}) if card_ids else []

            cards = []
            for info in infos or []:
                front = strip_style(str(info.get("question") or ""))
                back = split_rendered_answer(str(info.get("answer") or ""))
                fields = sorted((info.get("fields") or {}).values(), key=lambda f: f.get("order", 0))
                if not front and fields:
                    front = fields[0].get("value", "")
                    back = fields[1].get("value", "") if len(fields) > 1 else ""
                deck_raw = str(info.get("deckName") or "Default")
                next_reviews = info.get("nextReviews")
                cards.append(
                    {
                        "id": str(info.get("cardId")),
                        "nid": str(info.get("note")),
                        "deck": deck_raw.replace("::", " / "),
                        "deck_raw": deck_raw,
                        "notetype": str(info.get("modelName") or "Basic"),
                        "queue": int(info.get("queue", 0)),
                        "ivl": int(info.get("interval", 0)),
                        "reps": int(info.get("reps", 0)),
                        "front": front,
                        "back": back,
                        "next_reviews": next_reviews if isinstance(next_reviews, list) else [],
                    }
                )

            return {
                "source": "ankiconnect",
                "study_mode": study_mode,
                "decks": decks,
                "total_cards": len(find("-is:suspended")),
                "total_due": len(find("(is:due OR is:learn) -is:suspended")),
                "total_new": len(find("is:new -is:suspended")),
                "filtered_count": len(cards),
                "reviewed_today": int(ankiconnect("getNumCardsReviewedToday") or 0),
                "cards": cards,
            }
        except (OSError, ValueError, RuntimeError) as e:
            log(f"AnkiConnect query failed, using offline snapshot: {e}")
            return None

    def _load_anki_snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        db_path = self._anki_profile_dir() / "collection.anki2"
        if not db_path.exists():
            return [], []
        mtime = db_path.stat().st_mtime
        with self.lock:
            if self.anki_cache["mtime"] == mtime and self.anki_cache["cards"]:
                return self.anki_cache["decks"], self.anki_cache["cards"]

        cards: list[dict[str, Any]] = []
        deck_counts: dict[str, int] = {}
        # Read a copy so a running Anki Desktop never sees a lock from us.
        with tempfile.NamedTemporaryFile(suffix=".anki2") as tf:
            shutil.copy2(db_path, tf.name)
            con = sqlite3.connect(f"file:{tf.name}?mode=ro", uri=True)
            con.create_collation("unicase", lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()))
            try:
                deck_map = {did: name.replace("\x1f", "::") for did, name in con.execute("SELECT id, name FROM decks")}
                notetype_map = dict(con.execute("SELECT id, name FROM notetypes"))
                rows = con.execute(
                    """
                    SELECT c.id, c.nid, c.did, c.ord, c.queue, c.ivl, n.mid, n.flds
                    FROM cards c JOIN notes n ON n.id = c.nid
                    WHERE c.queue != -1
                    ORDER BY c.queue DESC, c.due ASC, c.id ASC
                    """
                )
                for cid, nid, did, card_ord, c_queue, ivl, mid, flds in rows:
                    parts = flds.split("\x1f") + ["", ""]
                    front_raw, back_raw, extra_raw = parts[0], parts[1], parts[2]
                    nt_name = notetype_map.get(mid, "Basic")
                    deck_raw = deck_map.get(did, "Default")

                    # "Lückentext" is the German cloze notetype name.
                    if "{{c" in front_raw or "cloze" in nt_name.lower() or "lücken" in nt_name.lower():
                        front_html = render_cloze(front_raw, card_ord, reveal=False)
                        back_html = render_cloze(front_raw, card_ord, reveal=True)
                        extra = back_raw
                    else:
                        reverse = card_ord == 1 and back_raw.strip()
                        front_html, back_html = (back_raw, front_raw) if reverse else (front_raw, back_raw)
                        extra = extra_raw
                    if extra.strip():
                        back_html += f'<div class="anki-extra">{extra}</div>'
                    if not front_html.strip() and not back_html.strip():
                        continue

                    deck_counts[deck_raw] = deck_counts.get(deck_raw, 0) + 1
                    cards.append(
                        {
                            "id": str(cid),
                            "nid": str(nid),
                            "deck": deck_raw.replace("::", " / "),
                            "deck_raw": deck_raw,
                            "notetype": nt_name,
                            "queue": c_queue,
                            "ivl": ivl,
                            "front": front_html,
                            "back": back_html,
                            "next_reviews": [],
                        }
                    )
            finally:
                con.close()

        decks = [
            {"name": name, "label": name.replace("::", " / "), "count": cnt, "due": cnt, "new": 0}
            for name, cnt in sorted(deck_counts.items())
        ]
        with self.lock:
            self.anki_cache = {"mtime": mtime, "decks": decks, "cards": cards}
        return decks, cards

    def _use_headless(self) -> bool:
        return self.anki_store.is_authenticated() and self.anki_store.col_path.exists()

    def get_anki_payload(self, deck_filter: str = "ALL", study_mode: str = "due") -> dict[str, Any]:
        with self.lock:
            stats = self._study_stats_locked()
            reviewed_offline = int(self.anki_progress["reviewed_today"])

        payload = None
        if self._use_headless():
            try:
                payload = self.anki_store.get_cards_payload(deck_filter, study_mode)
            except Exception as e:  # a broken cache shouldn't hide AnkiConnect or the snapshot
                log(f"Headless Anki query failed, falling back: {e}")
        if payload is None:
            payload = self._get_anki_via_connect(deck_filter, study_mode)
        if payload is None:
            decks, cards = self._load_anki_snapshot()
            filtered = [
                c for c in cards
                if deck_filter in ("", "ALL") or c["deck_raw"] == deck_filter or c["deck_raw"].startswith(deck_filter + "::")
            ]
            payload = {
                "source": "offline",
                "study_mode": study_mode,
                "decks": decks,
                "total_cards": len(cards),
                "total_due": len(cards),
                "total_new": 0,
                "filtered_count": len(filtered),
                "reviewed_today": reviewed_offline,
                "cards": filtered,
            }
        for card in payload["cards"]:
            card["front"] = rewrite_media_urls(card["front"])
            card["back"] = rewrite_media_urls(card["back"])
        return {**payload, **stats}

    def record_anki_review(self, card_id: str, ease: str) -> dict[str, Any]:
        rating = EASES.index(ease) + 1
        with self.lock:
            xp_stats = self._award_study_xp_locked(ease)

        if self._use_headless():
            try:
                with self.anki_store.session() as col:
                    AnkiStore.answer(col, int(card_id), rating)
                    reviewed_today = AnkiStore.reviewed_today(col)
            except Exception as e:  # e.g. the card was deleted on another device
                log(f"Headless Anki answer failed, falling back: {e}")
            else:
                self._schedule_headless_sync()
                return {"ok": True, "synced": True, "source": "headless", "reviewed_today": reviewed_today, **xp_stats}

        try:
            res = ankiconnect("answerCards", {"answers": [{"cardId": int(card_id), "ease": rating}]}, timeout=2.5)
            if isinstance(res, list) and res and res[0]:
                reviewed_today = int(ankiconnect("getNumCardsReviewedToday") or 0)
                return {"ok": True, "synced": True, "source": "ankiconnect", "reviewed_today": reviewed_today, **xp_stats}
        except (OSError, ValueError, RuntimeError) as e:
            log(f"AnkiConnect answerCards failed, recording offline: {e}")

        intervals = {"again": 45, "hard": 300, "good": 86400, "easy": 86400 * 4}
        now = time.time()
        with self.lock:
            self.anki_progress["reviewed_today"] = int(self.anki_progress["reviewed_today"]) + 1
            cards_map = self.anki_progress.setdefault("cards", {})
            cards_map[card_id] = {
                "reps": int(cards_map.get(card_id, {}).get("reps", 0)) + 1,
                "last_ease": ease,
                "reviewed_at": now,
                "due_at": now + intervals[ease],
            }
            self._save_anki_progress()
            return {"ok": True, "synced": False, "source": "offline", "reviewed_today": self.anki_progress["reviewed_today"], **xp_stats}


STATE = FocusState()


class SafariWatcher(threading.Thread):
    """Polls the active tab of every Safari window, only while blocking is active."""

    GET_TABS_SCRIPT = r'''
if application "Safari" is running then
    tell application "Safari"
        set outLines to {}
        set winCount to count of windows
        repeat with i from 1 to winCount
            try
                set t to current tab of window i
                set u to URL of t
                if u is not missing value then
                    set end of outLines to (i as text) & tab & u
                end if
            end try
        end repeat
        set AppleScript's text item delimiters to linefeed
        return outLines as text
    end tell
end if
return ""
'''

    REDIRECT_TAB_SCRIPT = r'''
on run argv
    set winIdx to (item 1 of argv) as integer
    set expectedUrl to item 2 of argv
    set targetUrl to item 3 of argv
    if application "Safari" is running then
        tell application "Safari"
            try
                set t to current tab of window winIdx
                if (URL of t) is expectedUrl then
                    set URL of t to targetUrl
                    return "redirected"
                end if
            end try
        end tell
    end if
    return "skipped"
end run
'''

    def __init__(self, state: FocusState) -> None:
        super().__init__(daemon=True)
        self.state = state
        self.stop_event = threading.Event()
        self._last_prune = time.time()
        self._last_osa_err = 0.0

    def _run_osascript(self, script: str, args: list[str] | None = None) -> str:
        try:
            proc = subprocess.run(["osascript", "-e", script, *(args or [])], capture_output=True, text=True, timeout=2.5)
        except subprocess.TimeoutExpired:
            error = "timed out"
        except OSError as e:
            error = str(e)
        else:
            if proc.returncode == 0:
                return proc.stdout.strip()
            error = f"rc={proc.returncode}: {proc.stderr.strip()}"
        now = time.time()
        if now - self._last_osa_err > 60:
            self._last_osa_err = now
            log(f"Safari AppleScript error ({error})")
        return ""

    def run(self) -> None:
        log("SafariWatcher thread started.")
        while not self.stop_event.is_set():
            now = time.time()
            if now - self._last_prune >= 2.0:
                self._last_prune = now
                with self.state.lock:
                    changed = self.state.prune_locked()
                if changed:
                    self.state.broadcast("state")

            if not self.state.is_blocking_active():
                self.stop_event.wait(0.6)
                continue

            port = int(self.state.config["port"])
            for line in self._run_osascript(self.GET_TABS_SCRIPT).splitlines():
                win_idx, sep, url = line.partition("\t")
                domain = self.state.match_blocked_domain(url) if sep else None
                if not domain:
                    continue
                started = self.state.record_intercept(domain, url)
                query = urllib.parse.urlencode({"from": url, "domain": domain, "t": str(int(started))})
                if self._run_osascript(self.REDIRECT_TAB_SCRIPT, [win_idx.strip(), url, f"http://{HOST}:{port}/?{query}"]) == "redirected":
                    log(f"Intercepted Safari tab: {domain} ({url[:80]})")

            self.stop_event.wait(max(0.25, float(self.state.config.get("poll_interval_ms", 450)) / 1000.0))


def post_route(path: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        RequestHandler.POST_ROUTES[path] = fn
        return fn

    return register


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    POST_ROUTES: dict[str, Callable[..., tuple[int, Any]]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _allowed_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"{HOST}:{port}", f"localhost:{port}"}

    def _foreign_request(self) -> bool:
        """Reject DNS-rebinding (Host) and cross-site requests (Origin) from other web pages."""
        allowed = self._allowed_hosts()
        if self.headers.get("Host") not in allowed:
            return True
        origin = self.headers.get("Origin")
        return origin is not None and urllib.parse.urlparse(origin).netloc not in allowed

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("Invalid Content-Length") from None
        if length > MAX_BODY_BYTES:
            raise BadRequest("Request body too large")
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("Body must be JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("Body must be a JSON object")
        return data

    def _serve_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self._foreign_request():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send_json(STATE.snapshot())
        elif path == "/api/events":
            self._handle_sse()
        elif path == "/api/scratchpad":
            self._send_json({"text": SCRATCHPAD_PATH.read_text(encoding="utf-8") if SCRATCHPAD_PATH.exists() else ""})
        elif path == "/api/anki/account":
            self._send_json(STATE.anki_account_status())
        elif path == "/api/anki/cards":
            self._send_json(STATE.get_anki_payload(qs.get("deck", ["ALL"])[0], qs.get("mode", ["due"])[0]))
        elif path.startswith("/api/anki/media/"):
            self._serve_media(urllib.parse.unquote(path.removeprefix("/api/anki/media/")))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def _serve_media(self, name: str) -> None:
        if name in ("", ".", "..") or name != os.path.basename(name) or "\x00" in name:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        headless = STATE.anki_store.media_dir / name
        self._serve_file(headless if headless.exists() else STATE._anki_profile_dir() / "collection.media" / name)

    def do_POST(self) -> None:
        if self._foreign_request():
            self._send_json({"ok": False, "error": "Cross-origin request rejected"}, status=403)
            return
        parsed = urllib.parse.urlparse(self.path)
        route = self.POST_ROUTES.get(parsed.path)
        if route is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            status, data = route(self._read_json(), urllib.parse.parse_qs(parsed.query))
        except BadRequest as e:
            status, data = 400, {"ok": False, "error": str(e)}
        self._send_json(data, status)

    def _handle_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        client_q: queue.Queue[str] = queue.Queue(maxsize=32)
        with STATE.lock:
            STATE.sse_clients.append(client_q)
        try:
            self.wfile.write(f"event: state\ndata: {json.dumps(STATE.snapshot())}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    msg = client_q.get(timeout=15.0).encode("utf-8")
                except queue.Empty:
                    msg = b": keepalive\n\n"
                self.wfile.write(msg)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with STATE.lock:
                if client_q in STATE.sse_clients:
                    STATE.sse_clients.remove(client_q)


@post_route("/api/agent/start")
def post_agent_start(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    return 200, STATE.start_session(
        str(body.get("session_id") or f"anon-{int(time.time())}"),
        str(body.get("agent") or "agent"),
        pid=int_field(body, "pid"),
        cwd=str(body.get("cwd") or ""),
        prompt=str(body.get("prompt") or ""),
    )


@post_route("/api/agent/stop")
def post_agent_stop(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    return 200, STATE.stop_session(str(body.get("session_id") or ""))


@post_route("/api/agent/clear")
def post_agent_clear(_body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    return 200, STATE.clear_sessions()


@post_route("/api/focus")
def post_focus(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    return 200, STATE.set_manual_focus(bool(body.get("enabled")))


@post_route("/api/pause")
def post_pause(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    return 200, STATE.set_pause(int_field(body, "seconds"))


@post_route("/api/bypass")
def post_bypass(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    domain = str(body.get("domain") or "")
    if not normalize_domain(domain):
        raise BadRequest("domain is required")
    if body.get("remove"):
        return 200, STATE.clear_bypass(domain)
    remaining = STATE.add_bypass(domain, int_field(body, "seconds") or int(STATE.config.get("default_bypass_seconds", 120)))
    if remaining:
        return 429, {"ok": False, "error": "Emergency bypass locked", "remaining": remaining, "state": STATE.snapshot()}
    return 200, {"ok": True, "state": STATE.snapshot()}


@post_route("/api/config")
def post_config(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    with STATE.lock:
        cfg = STATE.config
        for key in ("auto_restore", "completion_chime"):
            if key in body:
                cfg[key] = bool(body[key])
        if "default_bypass_seconds" in body:
            cfg["default_bypass_seconds"] = max(15, int_field(body, "default_bypass_seconds"))
        if "bypass_cooldown_seconds" in body:
            cfg["bypass_cooldown_seconds"] = max(0, int_field(body, "bypass_cooldown_seconds"))
        if isinstance(body.get("blocked_domains"), list):
            cfg["blocked_domains"] = normalize_domains(body["blocked_domains"])
        if "add_domain" in body:
            cfg["blocked_domains"] = normalize_domains([body["add_domain"], *cfg["blocked_domains"]])
        if "remove_domain" in body:
            removed = normalize_domain(str(body["remove_domain"]))
            cfg["blocked_domains"] = [d for d in cfg["blocked_domains"] if d != removed]
        STATE.save_config()
    STATE.broadcast("state")
    return 200, STATE.snapshot()


@post_route("/api/scratchpad")
def post_scratchpad(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    text = str(body.get("text") or "")
    SCRATCHPAD_PATH.write_text(text, encoding="utf-8")
    return 200, {"ok": True, "length": len(text)}


@post_route("/api/scratchpad/obsidian")
def post_scratchpad_obsidian(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    text = str(body.get("text") or "").strip()
    if not text:
        return 400, {"ok": False, "error": "Scratchpad is empty"}
    raw_note = str(STATE.config.get("obsidian_note_path") or "").strip()
    if not raw_note:
        return 400, {"ok": False, "error": "Set obsidian_note_path in config.json"}
    note_path = Path(raw_note).expanduser()
    entry = f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{text}\n"
    try:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        if note_path.exists():
            with note_path.open("a", encoding="utf-8") as f:
                f.write(entry)
        else:
            note_path.write_text(f"# Agent Scratchpad\n{entry}", encoding="utf-8")
    except OSError as e:
        return 500, {"ok": False, "error": str(e)}
    return 200, {"ok": True, "path": str(note_path)}


@post_route("/api/anki/review")
def post_anki_review(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    card_id = str(body.get("card_id") or "")
    ease = str(body.get("ease") or "good")
    if not card_id.isdigit():
        raise BadRequest("card_id must be a numeric Anki card id")
    if ease not in EASES:
        raise BadRequest(f"ease must be one of {', '.join(EASES)}")
    return 200, STATE.record_anki_review(card_id, ease)


@post_route("/api/anki/login")
def post_anki_login(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    if not email or not password:
        raise BadRequest("Email and password are required")
    if not ANKI_AVAILABLE:
        return 503, {"ok": False, "error": "The anki package is not installed. Run ./install.sh or pip install anki."}
    try:
        return 200, STATE.login_anki(email, password, endpoint=str(body.get("endpoint") or "").strip() or None)
    except SyncError as e:
        return (401 if is_auth_error(e) else 502), {"ok": False, "error": str(e)}
    except (OSError, RuntimeError) as e:
        return 503, {"ok": False, "error": str(e)}


@post_route("/api/anki/logout")
def post_anki_logout(_body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    return 200, STATE.logout_anki()


@post_route("/api/anki/sync")
def post_anki_sync(body: dict[str, Any], qs: dict[str, list[str]]) -> tuple[int, Any]:
    auto_only = qs.get("auto", ["0"])[0] == "1"
    on_conflict = str(body.get("on_conflict") or "raise")
    if on_conflict not in ("raise", "download", "upload"):
        raise BadRequest("on_conflict must be raise, download, or upload")
    ok, info = STATE.flush_anki_sync(only_if_dirty=auto_only, media=bool(body.get("media", not auto_only)), on_conflict=on_conflict)
    if ok:
        return 200, {"ok": True, **info}
    return (409 if info.get("conflict") else 401 if info.get("auth_error") else 503), {"ok": False, **info}


@post_route("/api/focus-app")
def post_focus_app(body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    app_name = str(body.get("app") or STATE.config.get("return_app") or "Orca").strip()
    if not APP_NAME_RE.fullmatch(app_name):
        raise BadRequest("Invalid app name")
    try:
        subprocess.Popen(["/usr/bin/open", "-a", app_name])
    except OSError as e:
        return 500, {"ok": False, "error": str(e)}
    return 200, {"ok": True, "app": app_name}


@post_route("/api/anki/launch")
def post_anki_launch(_body: dict[str, Any], _qs: dict[str, list[str]]) -> tuple[int, Any]:
    try:
        subprocess.Popen(["/usr/bin/open", "-g", "-j", "-a", "/Applications/Anki.app"])
    except OSError as e:
        return 500, {"ok": False, "error": str(e)}
    for _ in range(10):
        time.sleep(0.5)
        if ankiconnect_alive(timeout=0.5):
            return 200, {"ok": True, "ankiconnect": True}
    return 200, {"ok": True, "ankiconnect": False}


class FocusHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    port = int(STATE.config["port"])
    watcher = SafariWatcher(STATE)
    watcher.start()
    server = FocusHTTPServer((HOST, port), RequestHandler)
    log(f"Agent Focus listening on http://{HOST}:{port} (data: {DATA_DIR})")

    def handle_sig(signum: int, _frame: Any) -> None:
        log(f"Received signal {signum}, shutting down.")
        watcher.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)
    try:
        server.serve_forever()
    finally:
        watcher.stop_event.set()
        STATE.flush_anki_sync(only_if_dirty=True, media=False)
        server.server_close()


if __name__ == "__main__":
    main()
