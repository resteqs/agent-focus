#!/usr/bin/env python3
"""
Agent Focus — Local Wait-State Companion, Safari Tab Watcher & Two-Way AnkiConnect Bridge.

Binds strictly to 127.0.0.1 and watches Safari's active tab via macOS AppleScript
only while a coding agent (Claude Code, Codex, Antigravity) is actively working.
"""

from __future__ import annotations

import html
import json
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ankistore import ANKI_AVAILABLE, AnkiStore, FullSyncConflict, SyncError

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(
    os.environ.get("AGENT_FOCUS_DATA_DIR") or (Path.home() / ".local/share/agent-focus")
).expanduser()
CONFIG_PATH = DATA_DIR / "config.json"
SCRATCHPAD_PATH = DATA_DIR / "scratchpad.md"
ANKI_PROGRESS_PATH = DATA_DIR / "anki_progress.json"
ANKICONNECT_URL = os.environ.get("AGENT_FOCUS_ANKICONNECT_URL", "http://127.0.0.1:8765")

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


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        sys.stderr.write(f"[{ts}] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def normalize_domain(raw: str) -> str:
    raw = raw.strip().lower()
    if "://" in raw:
        raw = urllib.parse.urlparse(raw).hostname or ""
    else:
        raw = raw.split("/")[0].split(":")[0]
    if raw.startswith("www."):
        raw = raw[4:]
    return raw.strip(".")


def pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


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


class FocusState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.config: dict[str, Any] = self._load_config()
        self.sessions: dict[str, dict[str, Any]] = {}
        self.bypasses: dict[str, float] = {}
        self.intercept_started: dict[str, float] = {}
        self.paused_until: float = 0.0
        self.manual_focus: bool = False
        self.last_intercept: dict[str, Any] | None = None
        self.intercept_count_today: int = 0
        self.intercept_day: str = datetime.now().strftime("%Y-%m-%d")
        self.sse_clients: list[queue.Queue[str]] = []
        self.anki_cache: dict[str, Any] = {"mtime": 0.0, "decks": [], "cards": []}
        self.anki_progress: dict[str, Any] = self._load_anki_progress()
        self._anki_sync_timer: threading.Timer | None = None
        self._anki_dirty: bool = False
        self.anki_store = AnkiStore(
            home=str(self.config.get("anki_headless_home") or "~/.local/share/anki-headless"),
            endpoint=str(self.config.get("anki_sync_endpoint") or "").strip() or None,
            profile=str(self.config.get("anki_headless_profile") or "agent-focus"),
        )

    def _load_config(self) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception as e:
                log(f"Config load error: {e}")
        domains = []
        seen = set()
        for d in cfg.get("blocked_domains", []):
            nd = normalize_domain(str(d))
            if nd and nd not in seen:
                seen.add(nd)
                domains.append(nd)
        cfg["blocked_domains"] = domains
        return cfg

    def save_config(self) -> None:
        with self.lock:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.config, indent=2) + "\n", encoding="utf-8")
            tmp.replace(CONFIG_PATH)

    def _load_anki_progress(self) -> dict[str, Any]:
        if ANKI_PROGRESS_PATH.exists():
            try:
                data = json.loads(ANKI_PROGRESS_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data.setdefault("day", datetime.now().strftime("%Y-%m-%d"))
                    data.setdefault("reviewed_today", 0)
                    data.setdefault("xp_today", 0)
                    data.setdefault("combo", 0)
                    data.setdefault("streak_days", 0)
                    data.setdefault("last_study_day", "")
                    data.setdefault("cards", {})
                    return data
            except Exception:
                pass
        return {
            "day": datetime.now().strftime("%Y-%m-%d"),
            "reviewed_today": 0,
            "xp_today": 0,
            "combo": 0,
            "streak_days": 0,
            "last_study_day": "",
            "cards": {},
        }

    def _save_anki_progress(self) -> None:
        ANKI_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = ANKI_PROGRESS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.anki_progress, indent=2) + "\n", encoding="utf-8")
        tmp.replace(ANKI_PROGRESS_PATH)

    def broadcast(self, event_name: str = "state") -> None:
        payload = json.dumps(self.snapshot())
        msg = f"event: {event_name}\ndata: {payload}\n\n"
        with self.lock:
            dead = []
            for q in self.sse_clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                if q in self.sse_clients:
                    self.sse_clients.remove(q)

    def prune_locked(self) -> bool:
        now = time.time()
        ttl = float(self.config.get("session_ttl_seconds", 1500))
        changed = False
        was_active = bool(self.sessions) or self.manual_focus

        stale_keys = []
        for sid, info in self.sessions.items():
            pid = int(info.get("pid") or 0)
            age = now - float(info.get("updated_at") or info.get("started_at") or now)
            if age > ttl or (pid > 1 and not pid_alive(pid)):
                stale_keys.append(sid)
        for sid in stale_keys:
            self.sessions.pop(sid, None)
            changed = True

        expired_bypasses = [d for d, exp in self.bypasses.items() if exp <= now]
        for d in expired_bypasses:
            self.bypasses.pop(d, None)
            changed = True

        if self.paused_until and self.paused_until <= now:
            self.paused_until = 0.0
            changed = True

        now_active = bool(self.sessions) or self.manual_focus
        if was_active and not now_active:
            if self.intercept_started:
                self.intercept_started.clear()
                changed = True
            if self._anki_dirty and self.anki_store.is_authenticated():
                threading.Thread(
                    target=self.flush_anki_sync,
                    kwargs={"only_if_dirty": True, "media": False},
                    daemon=True,
                ).start()

        today = datetime.now().strftime("%Y-%m-%d")
        if self.intercept_day != today:
            self.intercept_day = today
            self.intercept_count_today = 0
            changed = True

        return changed

    def start_session(
        self,
        session_id: str,
        agent: str,
        pid: int = 0,
        cwd: str = "",
        prompt: str = "",
    ) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            self.prune_locked()
            existing = self.sessions.get(session_id)
            if existing:
                existing["updated_at"] = now
                if pid > 1:
                    existing["pid"] = pid
                if cwd:
                    existing["cwd"] = cwd
                    existing["project"] = Path(cwd).name
                if prompt:
                    existing["prompt"] = prompt[:160]
            else:
                self.sessions[session_id] = {
                    "session_id": session_id,
                    "agent": agent or "agent",
                    "pid": pid,
                    "cwd": cwd,
                    "project": Path(cwd).name if cwd else "",
                    "prompt": prompt[:160] if prompt else "",
                    "started_at": now,
                    "updated_at": now,
                }
        self.broadcast("state")
        return self.snapshot()

    def stop_session(self, session_id: str, agent: str = "") -> dict[str, Any]:
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
            self.manual_focus = bool(enabled)
            if enabled:
                self.paused_until = 0.0
            elif not self.sessions:
                self.intercept_started.clear()
        self.broadcast("state")
        return self.snapshot()

    def set_pause(self, seconds: int) -> dict[str, Any]:
        with self.lock:
            self.paused_until = time.time() + max(0, seconds) if seconds > 0 else 0.0
        self.broadcast("state")
        return self.snapshot()

    def bypass_cooldown_remaining(self, domain: str) -> int:
        nd = normalize_domain(domain)
        if not nd:
            return 0
        cooldown = int(self.config.get("bypass_cooldown_seconds", 30))
        with self.lock:
            now = time.time()
            started = self.intercept_started.get(nd)
            if started is None:
                self.intercept_started[nd] = now
                return cooldown
            return max(0, int(cooldown - (now - started) + 0.999))

    def add_bypass(self, domain: str, seconds: int) -> tuple[bool, int, dict[str, Any]]:
        nd = normalize_domain(domain)
        if not nd:
            return False, 0, self.snapshot()
        remaining = self.bypass_cooldown_remaining(nd)
        if remaining > 0:
            return False, remaining, self.snapshot()
        with self.lock:
            self.bypasses[nd] = time.time() + max(10, seconds)
            self.intercept_started.pop(nd, None)
        self.broadcast("state")
        return True, 0, self.snapshot()

    def clear_bypass(self, domain: str) -> dict[str, Any]:
        nd = normalize_domain(domain)
        with self.lock:
            self.bypasses.pop(nd, None)
        self.broadcast("state")
        return self.snapshot()

    def is_blocking_active(self) -> bool:
        with self.lock:
            self.prune_locked()
            now = time.time()
            if self.paused_until > now:
                return False
            return bool(self.sessions) or self.manual_focus

    def match_blocked_domain(self, url: str) -> str | None:
        if not url:
            return None
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        if not host or host in ("localhost", "127.0.0.1", "::1"):
            return None
        clean_host = host[4:] if host.startswith("www.") else host

        with self.lock:
            now = time.time()
            for bdom, exp in list(self.bypasses.items()):
                if exp > now and (clean_host == bdom or clean_host.endswith("." + bdom)):
                    return None
            for dom in self.config.get("blocked_domains", []):
                if clean_host == dom or clean_host.endswith("." + dom):
                    return dom
        return None

    def record_intercept(self, domain: str, url: str) -> float:
        nd = normalize_domain(domain)
        now = time.time()
        with self.lock:
            today = datetime.now().strftime("%Y-%m-%d")
            if self.intercept_day != today:
                self.intercept_day = today
                self.intercept_count_today = 0
            self.intercept_count_today += 1
            started = self.intercept_started.get(nd)
            if started is None:
                started = now
                self.intercept_started[nd] = started
            self.last_intercept = {"domain": nd, "url": url, "at": now}
        self.broadcast("intercept")
        return started

    def _short_obsidian_label(self) -> str:
        raw = str(self.config.get("obsidian_note_path") or "").strip()
        if not raw:
            return ""
        p = Path(raw).expanduser()
        return f"{p.parent.name}/{p.name}" if p.parent.name else p.name

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self.prune_locked()
            now = time.time()
            active_list = sorted(self.sessions.values(), key=lambda s: s["started_at"])
            paused_remaining = max(0, int(self.paused_until - now)) if self.paused_until > now else 0
            blocking = (bool(active_list) or self.manual_focus) and paused_remaining == 0
            bypasses = {d: max(0, int(exp - now)) for d, exp in self.bypasses.items() if exp > now}
            cooldown = int(self.config.get("bypass_cooldown_seconds", 30))
            cooldowns = {
                d: max(0, int(cooldown - (now - ts) + 0.999))
                for d, ts in self.intercept_started.items()
            }
            earliest_start = min((s["started_at"] for s in active_list), default=None)
            return {
                "now": now,
                "blocking": blocking,
                "manual_focus": self.manual_focus,
                "paused_remaining": paused_remaining,
                "active_count": len(active_list),
                "agents": active_list,
                "earliest_start": earliest_start,
                "bypasses": bypasses,
                "bypass_cooldowns": cooldowns,
                "intercept_count_today": self.intercept_count_today,
                "last_intercept": self.last_intercept,
                "config": {
                    "auto_restore": bool(self.config.get("auto_restore", False)),
                    "return_app": str(self.config.get("return_app") or "Orca"),
                    "completion_chime": bool(self.config.get("completion_chime", True)),
                    "default_bypass_seconds": int(self.config.get("default_bypass_seconds", 120)),
                    "bypass_cooldown_seconds": cooldown,
                    "blocked_domains": list(self.config.get("blocked_domains", [])),
                    "obsidian_configured": bool(str(self.config.get("obsidian_note_path") or "").strip()),
                    "obsidian_label": self._short_obsidian_label(),
                    "anki_headless_available": ANKI_AVAILABLE,
                    "anki_headless_signed_in": self.anki_store.is_authenticated(),
                },
            }

    # --- Anki helpers (Two-way AnkiConnect + safe read-only fallback) ---

    def _anki_db_path(self) -> Path:
        return Path(self.config.get("anki_profile_dir", DEFAULT_CONFIG["anki_profile_dir"])).expanduser() / "collection.anki2"

    def _anki_media_dir(self) -> Path:
        return Path(self.config.get("anki_profile_dir", DEFAULT_CONFIG["anki_profile_dir"])).expanduser() / "collection.media"

    def _rewrite_media_urls(self, markup: str) -> str:
        def repl(match: re.Match[str]) -> str:
            quote = match.group(1)
            src = match.group(2)
            if src.startswith(("http://", "https://", "data:", "/api/")):
                return match.group(0)
            safe_name = urllib.parse.quote(os.path.basename(src))
            return f"src={quote}/api/anki/media/{safe_name}{quote}"

        return re.sub(r'src=(["\'])([^"\']+)\1', repl, markup)

    def _strip_anki_style(self, markup: str) -> str:
        cleaned = re.sub(r"<style[^>]*>.*?</style>", "", markup, flags=re.IGNORECASE | re.DOTALL)
        cleaned = re.sub(r"<script[^>]*>.*?</script>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        return cleaned.strip()

    def _render_cloze(self, text: str, card_ord: int, reveal: bool) -> str:
        target_idx = card_ord + 1

        def repl(match: re.Match[str]) -> str:
            idx = int(match.group(1))
            answer = match.group(2)
            hint = match.group(3)
            if idx == target_idx:
                if reveal:
                    return f'<span class="cloze-reveal">{answer}</span>'
                placeholder = html.escape(hint) if hint else "…"
                return f'<span class="cloze-blank">[{placeholder}]</span>'
            return answer

        return re.sub(r"\{\{c(\d+)::([^:}]+)(?:::([^}]+))?\}\}", repl, text)

    def anki_account_status(self) -> dict[str, Any]:
        info = self.anki_store.auth_info()
        if info.get("signed_in"):
            mode = "headless"
        else:
            mode = "offline"
            try:
                if ankiconnect("version", timeout=0.35):
                    mode = "ankiconnect"
            except Exception:
                pass
        return {
            **info,
            "mode": mode,
            "endpoint_label": info.get("endpoint") or "AnkiWeb",
        }

    def login_anki(self, email: str, password: str, endpoint: str | None = None) -> dict[str, Any]:
        if not ANKI_AVAILABLE:
            raise RuntimeError("Headless Anki package is not installed in this Python environment.")
        ep = (endpoint or "").strip() or None
        self.anki_store.login(email, password, endpoint=ep)
        # Always pull before any local write on a fresh cache to avoid FULL_SYNC conflicts
        sync_res = self.anki_store.sync(media=True, on_conflict="download")
        with self.lock:
            self._anki_dirty = False
            self.config["anki_sync_endpoint"] = ep or ""
            self.save_config()
        self.broadcast("state")
        return {"ok": True, "account": self.anki_account_status(), "sync": sync_res}

    def logout_anki(self) -> dict[str, Any]:
        with self.lock:
            if self._anki_sync_timer:
                self._anki_sync_timer.cancel()
                self._anki_sync_timer = None
            self._anki_dirty = False
        self.anki_store.logout()
        self.broadcast("state")
        return {"ok": True, "account": self.anki_account_status()}

    def flush_anki_sync(
        self,
        only_if_dirty: bool = True,
        media: bool = True,
        on_conflict: str = "raise",
    ) -> tuple[bool, dict[str, Any]]:
        with self.lock:
            if only_if_dirty and not self._anki_dirty:
                return True, {"skipped": True}
            if self._anki_sync_timer:
                self._anki_sync_timer.cancel()
                self._anki_sync_timer = None
            self._anki_dirty = False

        # Priority 1: Silent headless AnkiStore sync (zero GUI popups)
        if self.anki_store.is_authenticated():
            try:
                res = self.anki_store.sync(media=media, on_conflict=on_conflict)
                log(f"Headless Anki sync complete: {res.get('required')}")
                return True, {"mode": "headless", **res}
            except FullSyncConflict as e:
                log(f"Headless Anki full-sync conflict: {e}")
                return False, {"error": str(e), "conflict": True, "mode": "headless"}
            except SyncError as e:
                log(f"Headless Anki sync error: {e}")
                return False, {"error": str(e), "auth_error": "incorrect" in str(e).lower(), "mode": "headless"}
            except Exception as e:
                log(f"Headless Anki sync failed: {e}")
                return False, {"error": str(e), "mode": "headless"}

        # Never trigger AnkiConnect's popup-inducing mw.onSync() automatically
        if only_if_dirty:
            return True, {"skipped": True}

        try:
            ankiconnect("sync", timeout=10.0)
            log("AnkiConnect AnkiWeb sync complete.")
            return True, {"mode": "ankiconnect"}
        except Exception as e:
            log(f"AnkiConnect sync skipped: {e}")
            return False, {"error": str(e), "mode": "ankiconnect"}

    def _schedule_headless_sync(self) -> None:
        if not self.anki_store.is_authenticated():
            return
        with self.lock:
            self._anki_dirty = True
            if self._anki_sync_timer:
                self._anki_sync_timer.cancel()
            # Gentle >=60s debounce; silent in-process rslib sync (zero GUI popups)
            t = threading.Timer(
                65.0,
                self.flush_anki_sync,
                kwargs={"only_if_dirty": True, "media": False},
            )
            t.daemon = True
            t.start()
            self._anki_sync_timer = t

    def _schedule_ankiconnect_sync(self) -> None:
        # Reviews are already committed immediately to Anki Desktop via answerCards.
        # Avoid calling AnkiConnect's `sync` automatically because it invokes mw.onSync(),
        # which opens an intrusive macOS "Sync complete" dialog even when Anki is hidden.
        with self.lock:
            self._anki_dirty = True

    def _study_stats_locked(self) -> dict[str, int]:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.anki_progress.get("day") != today:
            self.anki_progress["day"] = today
            self.anki_progress["reviewed_today"] = 0
            self.anki_progress["xp_today"] = 0
            self.anki_progress["combo"] = 0
            self._save_anki_progress()
        return {
            "xp_today": int(self.anki_progress.get("xp_today", 0)),
            "combo": int(self.anki_progress.get("combo", 0)),
            "streak_days": int(self.anki_progress.get("streak_days", 0)),
        }

    def _award_study_xp_locked(self, ease: str) -> dict[str, int]:
        today_dt = datetime.now().date()
        today = today_dt.isoformat()
        if self.anki_progress.get("day") != today:
            self.anki_progress["day"] = today
            self.anki_progress["reviewed_today"] = 0
            self.anki_progress["xp_today"] = 0
            self.anki_progress["combo"] = 0

        last_day = str(self.anki_progress.get("last_study_day") or "")
        if last_day != today:
            if last_day:
                try:
                    prev_dt = datetime.strptime(last_day, "%Y-%m-%d").date()
                    delta_days = (today_dt - prev_dt).days
                    self.anki_progress["streak_days"] = (
                        int(self.anki_progress.get("streak_days", 0)) + 1 if delta_days == 1 else 1
                    )
                except ValueError:
                    self.anki_progress["streak_days"] = 1
            else:
                self.anki_progress["streak_days"] = 1
            self.anki_progress["last_study_day"] = today

        base_xp = {"again": 0, "hard": 10, "good": 15, "easy": 20}.get(ease, 15)
        if ease == "again":
            self.anki_progress["combo"] = 0
            gained = 0
        else:
            combo = int(self.anki_progress.get("combo", 0)) + 1
            self.anki_progress["combo"] = combo
            bonus = 5 if combo >= 3 else 0
            gained = base_xp + bonus
            self.anki_progress["xp_today"] = int(self.anki_progress.get("xp_today", 0)) + gained

        self._save_anki_progress()
        return {
            "gained_xp": gained,
            "xp_today": int(self.anki_progress.get("xp_today", 0)),
            "combo": int(self.anki_progress.get("combo", 0)),
            "streak_days": int(self.anki_progress.get("streak_days", 0)),
        }

    def _get_anki_via_connect(self, deck_filter: str, study_mode: str) -> dict[str, Any] | None:
        try:
            ver = ankiconnect("version", timeout=0.6)
            if not ver:
                return None
        except Exception:
            return None

        try:
            deck_names: list[str] = ankiconnect("deckNames") or []
            reviewed_today: int = int(ankiconnect("getNumCardsReviewedToday") or 0)
            deck_stats: dict[str, Any] = ankiconnect("getDeckStats", {"decks": deck_names}) or {}

            decks_out = []
            for dname in sorted(deck_names):
                st = next((v for v in deck_stats.values() if v.get("name") == dname), {})
                due_n = int(st.get("review_count", 0))
                learn_n = int(st.get("learn_count", 0))
                new_n = int(st.get("new_count", 0))
                total_in_deck = int(st.get("total_in_deck", 0))
                if total_in_deck == 0 and dname == "Default" and len(deck_names) > 1:
                    continue
                decks_out.append(
                    {
                        "name": dname,
                        "label": dname.replace("::", " / "),
                        "count": total_in_deck,
                        "due": due_n + learn_n,
                        "new": new_n,
                    }
                )

            all_due_ids: list[int] = ankiconnect("findCards", {"query": "(is:due OR is:learn) -is:suspended"}) or []
            all_new_ids: list[int] = ankiconnect("findCards", {"query": "is:new -is:suspended"}) or []
            all_ids: list[int] = ankiconnect("findCards", {"query": "-is:suspended"}) or []

            deck_clause = ""
            if deck_filter and deck_filter != "ALL":
                escaped_deck = deck_filter.replace('"', '\\"')
                deck_clause = f'deck:"{escaped_deck}" '

            if study_mode == "all":
                card_ids: list[int] = ankiconnect("findCards", {"query": f"{deck_clause}-is:suspended"}) or []
            else:
                due_ids: list[int] = ankiconnect("findCards", {"query": f"{deck_clause}(is:due OR is:learn) -is:suspended"}) or []
                new_ids: list[int] = ankiconnect("findCards", {"query": f"{deck_clause}is:new -is:suspended"}) or []
                seen: set[int] = set()
                card_ids = []
                for cid in due_ids + new_ids:
                    if cid not in seen:
                        seen.add(cid)
                        card_ids.append(cid)

            card_ids = card_ids[:100]
            infos = ankiconnect("cardsInfo", {"cards": card_ids}) if card_ids else []

            cards_out = []
            for info in infos or []:
                raw_q = self._strip_anki_style(str(info.get("question") or ""))
                raw_a = self._strip_anki_style(str(info.get("answer") or ""))
                hr_split = re.split(r'<hr\s+id=["\']?answer["\']?\s*/?>', raw_a, maxsplit=1, flags=re.IGNORECASE)
                back_html = hr_split[1].strip() if len(hr_split) > 1 else raw_a

                fields = info.get("fields") or {}
                if not raw_q and fields:
                    ordered = sorted(fields.values(), key=lambda f: f.get("order", 0))
                    raw_q = ordered[0].get("value", "") if len(ordered) > 0 else ""
                    back_html = ordered[1].get("value", "") if len(ordered) > 1 else ""

                next_reviews = info.get("nextReviews") or []
                cards_out.append(
                    {
                        "id": str(info.get("cardId")),
                        "nid": str(info.get("note")),
                        "deck": str(info.get("deckName") or "Default").replace("::", " / "),
                        "deck_raw": str(info.get("deckName") or "Default"),
                        "notetype": str(info.get("modelName") or "Basic"),
                        "queue": int(info.get("queue", 0)),
                        "ivl": int(info.get("interval", 0)),
                        "reps": int(info.get("reps", 0)),
                        "front": self._rewrite_media_urls(raw_q),
                        "back": self._rewrite_media_urls(back_html),
                        "next_reviews": next_reviews if isinstance(next_reviews, list) else [],
                    }
                )

            return {
                "source": "ankiconnect",
                "ankiconnect": True,
                "study_mode": study_mode,
                "decks": decks_out,
                "total_cards": len(all_ids),
                "total_due": len(all_due_ids),
                "total_new": len(all_new_ids),
                "filtered_count": len(cards_out),
                "reviewed_today": reviewed_today,
                "cards": cards_out,
            }
        except Exception as e:
            log(f"AnkiConnect query failed, falling back to snapshot: {e}")
            return None

    def _load_anki_snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        db_path = self._anki_db_path()
        if not db_path.exists():
            return [], []
        mtime = db_path.stat().st_mtime
        with self.lock:
            if self.anki_cache["mtime"] == mtime and self.anki_cache["cards"]:
                return self.anki_cache["decks"], self.anki_cache["cards"]

        decks_out: list[dict[str, Any]] = []
        cards_out: list[dict[str, Any]] = []

        with tempfile.NamedTemporaryFile(suffix=".anki2", delete=True) as tf:
            shutil.copy2(db_path, tf.name)
            con = sqlite3.connect(f"file:{tf.name}?mode=ro", uri=True)
            con.create_collation(
                "unicase",
                lambda a, b: (a.lower() > b.lower()) - (a.lower() < b.lower()),
            )
            try:
                deck_map = {did: name.replace("\x1f", "::") for did, name in con.execute("SELECT id, name FROM decks")}
                notetype_map = dict(con.execute("SELECT id, name FROM notetypes"))
                deck_counts: dict[str, int] = {}
                query = """
                    SELECT c.id, c.nid, c.did, c.ord, c.queue, c.ivl, n.mid, n.flds
                    FROM cards c JOIN notes n ON n.id = c.nid
                    WHERE c.queue != -1
                    ORDER BY c.queue DESC, c.due ASC, c.id ASC
                """
                for cid, nid, did, card_ord, c_queue, ivl, mid, flds in con.execute(query):
                    parts = flds.split("\x1f")
                    nt_name = notetype_map.get(mid, "Basic")
                    deck_raw = deck_map.get(did, "Default")
                    front_raw = parts[0] if len(parts) > 0 else ""
                    back_raw = parts[1] if len(parts) > 1 else ""
                    extra_raw = parts[2] if len(parts) > 2 else ""

                    is_cloze = "{{c" in front_raw or "cloze" in nt_name.lower() or "lücken" in nt_name.lower()
                    if is_cloze:
                        front_html = self._render_cloze(front_raw, card_ord, reveal=False)
                        back_html = self._render_cloze(front_raw, card_ord, reveal=True)
                        if back_raw.strip():
                            back_html += f'<div class="anki-extra">{back_raw}</div>'
                    else:
                        if card_ord == 1 and back_raw.strip():
                            front_html, back_html = back_raw, front_raw
                        else:
                            front_html, back_html = front_raw, back_raw
                        if extra_raw.strip():
                            back_html += f'<div class="anki-extra">{extra_raw}</div>'

                    front_html = self._rewrite_media_urls(front_html)
                    back_html = self._rewrite_media_urls(back_html)
                    if not front_html.strip() and not back_html.strip():
                        continue
                    deck_counts[deck_raw] = deck_counts.get(deck_raw, 0) + 1
                    cards_out.append(
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
                for name, cnt in sorted(deck_counts.items()):
                    decks_out.append({"name": name, "label": name.replace("::", " / "), "count": cnt, "due": cnt, "new": 0})
            finally:
                con.close()

        with self.lock:
            self.anki_cache = {"mtime": mtime, "decks": decks_out, "cards": cards_out}
        return decks_out, cards_out

    def get_anki_payload(self, deck_filter: str = "ALL", study_mode: str = "due") -> dict[str, Any]:
        with self.lock:
            stats = self._study_stats_locked()

        # Priority 1: Direct headless AnkiStore (no Anki Desktop needed)
        if self.anki_store.is_authenticated() and self.anki_store.col_path.exists():
            try:
                payload = self.anki_store.get_cards_payload(deck_filter, study_mode)
                for c in payload.get("cards", []):
                    c["front"] = self._rewrite_media_urls(c["front"])
                    c["back"] = self._rewrite_media_urls(c["back"])
                payload.update(stats)
                return payload
            except Exception as e:
                log(f"Headless Anki query failed, falling back: {e}")

        # Priority 2: Local AnkiConnect (optional fallback)
        live = self._get_anki_via_connect(deck_filter, study_mode)
        if live is not None:
            live["anki_mode"] = "ankiconnect"
            live.update(stats)
            return live

        # Priority 3: Read-only offline SQLite snapshot
        decks, cards = self._load_anki_snapshot()
        with self.lock:
            reviewed_today = int(self.anki_progress.get("reviewed_today", 0))

        filtered = [
            c for c in cards
            if deck_filter in ("", "ALL") or c["deck_raw"] == deck_filter or c["deck_raw"].startswith(deck_filter + "::")
        ]
        return {
            "source": "offline",
            "ankiconnect": False,
            "anki_mode": "offline",
            "study_mode": study_mode,
            "decks": decks,
            "total_cards": len(cards),
            "total_due": len(cards),
            "total_new": 0,
            "filtered_count": len(filtered),
            "reviewed_today": reviewed_today,
            **stats,
            "cards": filtered,
        }

    def record_anki_review(self, card_id: str, ease: str) -> dict[str, Any]:
        ease_map = {"again": 1, "hard": 2, "good": 3, "easy": 4}
        ease_int = ease_map.get(ease, 3)

        with self.lock:
            xp_stats = self._award_study_xp_locked(ease)

        # Priority 1: Direct headless AnkiStore (exact FSRS/SM-2 + silent debounced sync)
        if self.anki_store.is_authenticated() and self.anki_store.col_path.exists():
            try:
                with self.anki_store.session() as col:
                    AnkiStore.answer(col, int(card_id), ease_int)
                    reviewed_today = AnkiStore.reviewed_today(col)
                self._schedule_headless_sync()
                return {
                    "ok": True,
                    "synced": True,
                    "source": "headless",
                    "reviewed_today": reviewed_today,
                    **xp_stats,
                }
            except Exception as e:
                log(f"Headless Anki answer failed, falling back: {e}")

        # Priority 2: Local AnkiConnect fallback
        try:
            res = ankiconnect("answerCards", {"answers": [{"cardId": int(card_id), "ease": ease_int}]}, timeout=2.5)
            ok = isinstance(res, list) and bool(res[0])
            if ok:
                reviewed_today = int(ankiconnect("getNumCardsReviewedToday") or 0)
                self._schedule_ankiconnect_sync()
                return {
                    "ok": True,
                    "synced": True,
                    "source": "ankiconnect",
                    "reviewed_today": reviewed_today,
                    **xp_stats,
                }
        except Exception as e:
            log(f"AnkiConnect answerCards fallback: {e}")

        intervals = {"again": 45, "hard": 300, "good": 86400, "easy": 86400 * 4}
        now = time.time()
        with self.lock:
            self.anki_progress["reviewed_today"] = int(self.anki_progress.get("reviewed_today", 0)) + 1
            cards_map = self.anki_progress.setdefault("cards", {})
            prev = cards_map.get(card_id, {})
            cards_map[card_id] = {
                "reps": int(prev.get("reps", 0)) + 1,
                "last_ease": ease,
                "reviewed_at": now,
                "due_at": now + intervals.get(ease, 86400),
            }
            self._save_anki_progress()
            return {
                "ok": True,
                "synced": False,
                "source": "offline",
                "reviewed_today": self.anki_progress["reviewed_today"],
                **xp_stats,
            }


STATE = FocusState()


class SafariWatcher(threading.Thread):
    """Polls Safari's active tab per window only while blocking is active."""

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
        cmd = ["osascript", "-e", script]
        if args:
            cmd.extend(args)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.5)
            if proc.returncode != 0:
                now = time.time()
                if now - self._last_osa_err > 60:
                    self._last_osa_err = now
                    log(f"Safari AppleScript error (rc={proc.returncode}): {proc.stderr.strip()}")
                return ""
            return proc.stdout.strip()
        except Exception as e:
            now = time.time()
            if now - self._last_osa_err > 60:
                self._last_osa_err = now
                log(f"Safari AppleScript exception: {e}")
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

            raw = self._run_osascript(self.GET_TABS_SCRIPT)
            if raw:
                port = int(self.state.config.get("port", 7842))
                for line in raw.splitlines():
                    if "\t" not in line:
                        continue
                    idx_str, url = line.split("\t", 1)
                    domain = self.state.match_blocked_domain(url)
                    if domain:
                        started = self.state.record_intercept(domain, url)
                        q = urllib.parse.urlencode(
                            {"from": url, "domain": domain, "t": str(int(started))}
                        )
                        target = f"http://127.0.0.1:{port}/?{q}"
                        res = self._run_osascript(
                            self.REDIRECT_TAB_SCRIPT,
                            [idx_str.strip(), url, target],
                        )
                        if res == "redirected":
                            log(f"Intercepted Safari tab: {domain} ({url[:80]})")

            interval = max(0.25, float(self.state.config.get("poll_interval_ms", 450)) / 1000.0)
            self.stop_event.wait(interval)


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1024 * 1024))
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _serve_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return

        if path == "/api/status":
            self._send_json(STATE.snapshot())
            return

        if path == "/api/events":
            self._handle_sse()
            return

        if path == "/api/scratchpad":
            text = SCRATCHPAD_PATH.read_text(encoding="utf-8") if SCRATCHPAD_PATH.exists() else ""
            self._send_json({"text": text})
            return

        if path == "/api/anki/account":
            self._send_json(STATE.anki_account_status())
            return

        if path == "/api/anki/cards":
            deck = (qs.get("deck") or ["ALL"])[0]
            mode = (qs.get("mode") or ["due"])[0]
            self._send_json(STATE.get_anki_payload(deck, mode))
            return

        if path.startswith("/api/anki/media/"):
            raw_name = urllib.parse.unquote(path[len("/api/anki/media/"):])
            safe_name = os.path.basename(raw_name)
            if not safe_name or safe_name != raw_name or ".." in safe_name:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            headless_media = STATE.anki_store.media_dir / safe_name
            media_path = headless_media if headless_media.exists() else (STATE._anki_media_dir() / safe_name)
            self._serve_file(media_path)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json()

        if path == "/api/agent/start":
            sid = str(body.get("session_id") or f"anon-{int(time.time())}")
            agent = str(body.get("agent") or "agent")
            pid = int(body.get("pid") or 0)
            cwd = str(body.get("cwd") or "")
            prompt = str(body.get("prompt") or "")
            self._send_json(STATE.start_session(sid, agent, pid=pid, cwd=cwd, prompt=prompt))
            return

        if path == "/api/agent/stop":
            sid = str(body.get("session_id") or "")
            agent = str(body.get("agent") or "")
            self._send_json(STATE.stop_session(sid, agent=agent))
            return

        if path == "/api/agent/clear":
            self._send_json(STATE.clear_sessions())
            return

        if path == "/api/focus":
            self._send_json(STATE.set_manual_focus(bool(body.get("enabled", False))))
            return

        if path == "/api/pause":
            self._send_json(STATE.set_pause(int(body.get("seconds", 0))))
            return

        if path == "/api/bypass":
            domain = str(body.get("domain") or "")
            seconds = int(body.get("seconds") or STATE.config.get("default_bypass_seconds", 120))
            if body.get("remove"):
                self._send_json(STATE.clear_bypass(domain))
                return
            ok, remaining, snap = STATE.add_bypass(domain, seconds)
            if not ok:
                self._send_json(
                    {"ok": False, "error": "Emergency bypass locked", "remaining": remaining, "state": snap},
                    status=429,
                )
                return
            self._send_json({"ok": True, "state": snap})
            return

        if path == "/api/config":
            with STATE.lock:
                if "auto_restore" in body:
                    STATE.config["auto_restore"] = bool(body["auto_restore"])
                if "completion_chime" in body:
                    STATE.config["completion_chime"] = bool(body["completion_chime"])
                if "default_bypass_seconds" in body:
                    STATE.config["default_bypass_seconds"] = max(15, int(body["default_bypass_seconds"]))
                if "bypass_cooldown_seconds" in body:
                    STATE.config["bypass_cooldown_seconds"] = max(0, int(body["bypass_cooldown_seconds"]))
                if "blocked_domains" in body and isinstance(body["blocked_domains"], list):
                    cleaned = []
                    seen = set()
                    for d in body["blocked_domains"]:
                        nd = normalize_domain(str(d))
                        if nd and nd not in seen:
                            seen.add(nd)
                            cleaned.append(nd)
                    STATE.config["blocked_domains"] = cleaned
                if "add_domain" in body:
                    nd = normalize_domain(str(body["add_domain"]))
                    if nd and nd not in STATE.config["blocked_domains"]:
                        STATE.config["blocked_domains"].insert(0, nd)
                if "remove_domain" in body:
                    nd = normalize_domain(str(body["remove_domain"]))
                    STATE.config["blocked_domains"] = [
                        d for d in STATE.config["blocked_domains"] if d != nd
                    ]
                STATE.save_config()
            STATE.broadcast("state")
            self._send_json(STATE.snapshot())
            return

        if path == "/api/scratchpad":
            text = str(body.get("text") or "")
            SCRATCHPAD_PATH.parent.mkdir(parents=True, exist_ok=True)
            SCRATCHPAD_PATH.write_text(text, encoding="utf-8")
            self._send_json({"ok": True, "length": len(text)})
            return

        if path == "/api/scratchpad/obsidian":
            text = str(body.get("text") or "").strip()
            if not text:
                self._send_json({"ok": False, "error": "Scratchpad is empty"}, status=400)
                return
            raw_note = str(STATE.config.get("obsidian_note_path") or "").strip()
            if not raw_note:
                self._send_json({"ok": False, "error": "Set obsidian_note_path in config.json"}, status=400)
                return
            note_path = Path(raw_note).expanduser()
            try:
                note_path.parent.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                entry = f"\n## {stamp}\n\n{text}\n"
                if not note_path.exists():
                    note_path.write_text(f"# Agent Scratchpad\n{entry}", encoding="utf-8")
                else:
                    with note_path.open("a", encoding="utf-8") as f:
                        f.write(entry)
                self._send_json({"ok": True, "path": str(note_path)})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/anki/review":
            card_id = str(body.get("card_id") or "")
            ease = str(body.get("ease") or "good")
            if not card_id:
                self._send_json({"ok": False, "error": "Missing card_id"}, status=400)
                return
            self._send_json(STATE.record_anki_review(card_id, ease))
            return

        if path == "/api/anki/login":
            email = str(body.get("email") or "").strip()
            password = str(body.get("password") or "")
            endpoint = str(body.get("endpoint") or "").strip() or None
            if not email or not password:
                self._send_json({"ok": False, "error": "Email and password are required"}, status=400)
                return
            try:
                res = STATE.login_anki(email, password, endpoint=endpoint)
                self._send_json(res)
            except SyncError as e:
                self._send_json({"ok": False, "error": str(e)}, status=401)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/anki/logout":
            self._send_json(STATE.logout_anki())
            return

        if path == "/api/anki/sync":
            qs = urllib.parse.parse_qs(parsed.query)
            auto_only = (qs.get("auto") or ["0"])[0] == "1"
            on_conflict = str(body.get("on_conflict") or "raise")
            if on_conflict not in ("raise", "download", "upload"):
                on_conflict = "raise"
            media = bool(body.get("media", not auto_only))
            ok, info = STATE.flush_anki_sync(only_if_dirty=auto_only, media=media, on_conflict=on_conflict)
            if ok:
                self._send_json({"ok": True, **info})
            else:
                status = 409 if info.get("conflict") else (401 if info.get("auth_error") else 503)
                self._send_json({"ok": False, **info}, status=status)
            return

        if path == "/api/focus-app":
            app_name = str(body.get("app") or STATE.config.get("return_app") or "Orca").strip()
            if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,48}", app_name):
                self._send_json({"ok": False, "error": "Invalid app name"}, status=400)
                return
            try:
                subprocess.Popen(["/usr/bin/open", "-a", app_name])
                self._send_json({"ok": True, "app": app_name})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if path == "/api/anki/launch":
            try:
                subprocess.Popen(["/usr/bin/open", "-g", "-j", "-a", "/Applications/Anki.app"])
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        if ankiconnect("version", timeout=0.5):
                            self._send_json({"ok": True, "ankiconnect": True})
                            return
                    except Exception:
                        pass
                self._send_json({"ok": True, "ankiconnect": False})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

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
            init_msg = f"event: state\ndata: {json.dumps(STATE.snapshot())}\n\n"
            self.wfile.write(init_msg.encode("utf-8"))
            self.wfile.flush()

            while True:
                try:
                    msg = client_q.get(timeout=15.0)
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with STATE.lock:
                if client_q in STATE.sse_clients:
                    STATE.sse_clients.remove(client_q)


def main() -> None:
    port = int(STATE.config.get("port", 7842))
    watcher = SafariWatcher(STATE)
    watcher.start()

    class ReusableThreadingHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = ReusableThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    log(f"Agent Focus listening on http://127.0.0.1:{port} (data: {DATA_DIR})")

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
        if STATE._anki_dirty and STATE.anki_store.is_authenticated():
            STATE.flush_anki_sync(only_if_dirty=True, media=False)
        server.server_close()


if __name__ == "__main__":
    main()
