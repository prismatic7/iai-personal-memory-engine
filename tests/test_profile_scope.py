"""Profile scoping: session attribution and scope-env behaviour.

Regression cover for the fork's profile isolation. The invariants here are the
ones whose violation is SILENT — a wrong answer looks exactly like a right one:

  * an unknown session must return None, never a guessed profile (a guess would
    leak another profile's records into recall);
  * a non-string or empty profile must not become a tag (a malformed value
    would mint a bogus scope that matches nothing, or worse, matches "");
  * the "all" wildcard must disable scoping rather than scope to a profile
    literally named "all".

Attribution reads real Hermes state.db files, so these use tmp_path homes
rather than the developer's actual profiles.
"""

from __future__ import annotations

import sqlite3

import pytest

from iai_mcp import profile_scope


def _make_state_db(path, session_ids):
    """Create a minimal state.db with the `messages` table the reader uses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE messages (session_id TEXT)")
        conn.executemany(
            "INSERT INTO messages (session_id) VALUES (?)",
            [(s,) for s in session_ids],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A HOME with a default state.db and two named profiles."""
    monkeypatch.setattr(profile_scope.Path, "home", lambda: tmp_path)
    hermes = tmp_path / ".hermes"
    _make_state_db(hermes / "state.db", ["s-default-1", "s-default-2"])
    _make_state_db(hermes / "profiles" / "sysadmin" / "state.db", ["s-sys-1"])
    _make_state_db(hermes / "profiles" / "enodios" / "state.db", ["s-eno-1"])
    profile_scope.reset_cache()
    yield tmp_path
    profile_scope.reset_cache()


class TestSessionProfileMap:
    def test_maps_each_profile(self, fake_home):
        m = profile_scope.build_session_profile_map()
        assert m["s-default-1"] == "default"
        assert m["s-sys-1"] == "sysadmin"
        assert m["s-eno-1"] == "enodios"

    def test_unknown_session_is_none_not_a_guess(self, fake_home):
        # The load-bearing case: an unattributable session must NOT resolve to
        # whichever profile happens to be asking.
        assert profile_scope.session_profile_of("no-such-session") is None

    def test_empty_session_is_none(self, fake_home):
        assert profile_scope.session_profile_of("") is None
        assert profile_scope.session_profile_of(None) is None  # type: ignore[arg-type]

    def test_missing_state_db_degrades_without_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(profile_scope.Path, "home", lambda: tmp_path)
        profile_scope.reset_cache()
        assert profile_scope.build_session_profile_map() == {}
        assert profile_scope.session_profile_of("anything") is None
        profile_scope.reset_cache()

    def test_cache_reset_reflects_a_new_profile(self, fake_home):
        assert profile_scope.session_profile_of("s-sys-1") == "sysadmin"
        _make_state_db(
            fake_home / ".hermes" / "profiles" / "newprof" / "state.db", ["s-new-1"]
        )
        # Cached: the new session is not yet visible.
        assert profile_scope.session_profile_of("s-new-1") is None
        profile_scope.reset_cache()
        assert profile_scope.session_profile_of("s-new-1") == "newprof"


class TestProfileCacheSuffix:
    def test_unset_scope_is_unscoped(self, monkeypatch):
        monkeypatch.delenv("IAI_MCP_PROFILE", raising=False)
        assert profile_scope.profile_cache_suffix() == ""

    def test_all_wildcard_is_unscoped(self, monkeypatch):
        # "all" must DISABLE scoping, not scope to a profile named "all".
        monkeypatch.setenv("IAI_MCP_PROFILE", "all")
        assert profile_scope.profile_cache_suffix() == ""

    def test_named_scope_is_suffixed(self, monkeypatch):
        monkeypatch.setenv("IAI_MCP_PROFILE", "sysadmin")
        assert profile_scope.profile_cache_suffix() == ".sysadmin"

    def test_unsafe_characters_are_neutralised(self, monkeypatch):
        # A crafted name must not escape the store root.
        monkeypatch.setenv("IAI_MCP_PROFILE", "../../etc/passwd")
        sfx = profile_scope.profile_cache_suffix()
        assert "/" not in sfx and ".." not in sfx
        assert sfx.startswith(".")

    def test_scoped_cache_path_keeps_extension(self, monkeypatch):
        monkeypatch.setenv("IAI_MCP_PROFILE", "sysadmin")
        p = profile_scope.scoped_cache_path("/root", ".session-continuity.cached.md")
        assert str(p) == "/root/.session-continuity.cached.sysadmin.md"
        assert str(p).endswith(".md")

    def test_scoped_cache_path_unscoped_is_unchanged(self, monkeypatch):
        monkeypatch.delenv("IAI_MCP_PROFILE", raising=False)
        p = profile_scope.scoped_cache_path("/root", ".session-continuity.cached.md")
        assert str(p) == "/root/.session-continuity.cached.md"
