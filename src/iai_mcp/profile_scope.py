"""Session → Hermes profile attribution (fork).

A record captured before profile tagging existed still carries its originating
``session_id`` in provenance, but not the *profile* that session belonged to.
Hermes records that mapping itself: each profile owns a ``state.db`` whose
``messages`` table lists its session ids.

This module resolves ``session_id → profile`` by opening every profile's
``state.db`` read-only. It exists so recall can scope records that predate
tagging, without re-encrypting or rewriting anything.

Design constraints:

* **Cheap.** The map is read once and cached for the process. A recall path
  must not stat N databases per query.
* **Never fatal.** Any failure (missing db, locked db, no sqlite) degrades to
  "unknown", which the caller treats as unattributable — never as a match.
  A recall must not die because attribution could not be computed.
* **Read-only, always.** These are live session stores owned by another
  process; this module must never take a write lock on them.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

#: Cached ``session_id -> profile`` map for this process, or None if unbuilt.
_SESSION_PROFILE_CACHE: dict[str, str] | None = None

_DEFAULT_PROFILE = "default"
_STATE_DB = "state.db"


def _profiles_root() -> Path:
    return Path.home() / ".hermes" / "profiles"


def _iter_state_dbs() -> list[tuple[str, Path]]:
    """``(profile_name, state.db path)`` for every profile on this machine.

    The default profile's store lives at ``~/.hermes/state.db``; every other
    profile at ``~/.hermes/profiles/<name>/state.db``. HOME-anchored on
    purpose: this mirrors how Hermes itself resolves profiles, so it stays
    correct regardless of which profile is running.
    """
    out: list[tuple[str, Path]] = []
    default_db = Path.home() / ".hermes" / _STATE_DB
    if default_db.is_file():
        out.append((_DEFAULT_PROFILE, default_db))
    root = _profiles_root()
    try:
        for child in sorted(root.iterdir()):
            db = child / _STATE_DB
            if child.is_dir() and db.is_file():
                out.append((child.name, db))
    except OSError:
        pass
    return out


def build_session_profile_map() -> dict[str, str]:
    """Read every profile's ``state.db`` and return ``session_id -> profile``.

    Later profiles win on a collision, which cannot happen in practice: Hermes
    session ids are unique per store, and each session lives in exactly one.
    """
    mapping: dict[str, str] = {}
    for profile, db in _iter_state_dbs():
        try:
            # Read-only URI: never takes a write lock on a live session store.
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
            try:
                for (sid,) in conn.execute("SELECT DISTINCT session_id FROM messages"):
                    if sid:
                        mapping[str(sid)] = profile
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 -- attribution must never raise
            log.debug("session-profile map: skipping %s: %s", db, exc)
    return mapping


def session_profile_of(session_id: str) -> str | None:
    """Return the Hermes profile that owns *session_id*, or None if unknown.

    Unknown is a first-class answer: the caller must treat it as
    unattributable rather than guessing a profile.
    """
    global _SESSION_PROFILE_CACHE
    if not session_id:
        return None
    if _SESSION_PROFILE_CACHE is None:
        if os.environ.get("IAI_MCP_PROFILE_MAP_OFF") == "1":
            _SESSION_PROFILE_CACHE = {}
        else:
            try:
                _SESSION_PROFILE_CACHE = build_session_profile_map()
            except Exception as exc:  # noqa: BLE001 -- degrade, never die
                log.debug("session-profile map build failed: %s", exc)
                _SESSION_PROFILE_CACHE = {}
    return _SESSION_PROFILE_CACHE.get(str(session_id))


def reset_cache() -> None:
    """Drop the cached map (tests, and a long-lived process after a profile is added)."""
    global _SESSION_PROFILE_CACHE
    _SESSION_PROFILE_CACHE = None


# ── Profile-scoped cache paths ────────────────────────────────────────────────
#
# The daemon pre-warms a session-start payload and a continuity block as
# fixed-name files under the store root. Every profile's hook then reads the
# same file -- so a warmed cache serves one profile's memories to another,
# bypassing the recall-level filter entirely (the filter runs when the cache is
# BUILT, not when it is read).
#
# Under a profile scope, the cache name carries the profile. The daemon, which
# serves the store rather than a profile, keeps writing the unscoped name; a
# scoped reader therefore simply misses the cache and falls through to the live
# (filtered) recall path. That is the correct trade: a warm cache is an
# optimisation, and it must never be an isolation hole.

_UNSCOPED_SUFFIX = ""


def profile_cache_suffix() -> str:
    """``".<profile>"`` when a profile scope is active, else ``""``.

    Returns "" for an unset scope AND for the explicit "all" wildcard, so the
    unscoped cache keeps working when isolation is deliberately disabled.
    """
    raw = (os.environ.get("IAI_MCP_PROFILE") or "").strip()
    if not raw or raw == "all":
        return _UNSCOPED_SUFFIX
    # Keep it filesystem-safe: profile names are alnum/dash/underscore in
    # practice, but a stray character must not escape the store root.
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in raw)[:64]
    return f".{safe}" if safe else _UNSCOPED_SUFFIX


def scoped_cache_path(base: "Path | str", name: str) -> Path:
    """``<base>/.session-continuity.cached.md`` → ``...cached.<profile>.md``.

    The suffix is inserted before the final extension so the file keeps its
    ``.md``/``.json`` identity for anything that globs or reads by extension:
    ``.next-turn-pack.cached.md`` → ``.next-turn-pack.cached.default.md``.
    """
    base_p = Path(base)
    suffix = profile_cache_suffix()
    if not suffix:
        return base_p / name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return base_p / f"{name}{suffix}"
    return base_p / f"{stem}{suffix}.{ext}"

