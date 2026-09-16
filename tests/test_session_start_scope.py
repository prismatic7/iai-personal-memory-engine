"""The daemon must bind the request's profile scope for the payload compose.

The session-start payload is composed in the DAEMON's process, which serves
every profile from that one process. A caller's env var cannot reach it, so
the scope must arrive WITH the request and be restored afterwards.

Two invariants, both easy to break silently:

  * a request WITHOUT a profile composes unscoped (an older client must be
    unaffected — no new required param);
  * the binding is RESTORED, so a concurrent request cannot inherit it.
"""

from __future__ import annotations

import os

import pytest


class TestScopeBindingSemantics:
    """Exercise the save/restore shape the handler uses."""

    def _bind(self, requested: str | None):
        """Mirror of the handler's binding block, in isolation."""
        _req_profile = requested.strip() if isinstance(requested, str) else ""
        _prev = os.environ.get("IAI_MCP_PROFILE")
        if _req_profile:
            os.environ["IAI_MCP_PROFILE"] = _req_profile
        try:
            observed = os.environ.get("IAI_MCP_PROFILE")
        finally:
            if _req_profile:
                if _prev is None:
                    os.environ.pop("IAI_MCP_PROFILE", None)
                else:
                    os.environ["IAI_MCP_PROFILE"] = _prev
        return observed

    def test_no_profile_leaves_env_untouched(self, monkeypatch):
        monkeypatch.delenv("IAI_MCP_PROFILE", raising=False)
        assert self._bind(None) is None
        assert os.environ.get("IAI_MCP_PROFILE") is None

    def test_empty_profile_leaves_env_untouched(self, monkeypatch):
        monkeypatch.setenv("IAI_MCP_PROFILE", "default")
        assert self._bind("") == "default"
        assert os.environ.get("IAI_MCP_PROFILE") == "default"

    def test_whitespace_profile_is_not_a_scope(self, monkeypatch):
        monkeypatch.delenv("IAI_MCP_PROFILE", raising=False)
        assert self._bind("   ") is None
        assert os.environ.get("IAI_MCP_PROFILE") is None

    def test_scope_is_bound_then_restored(self, monkeypatch):
        monkeypatch.delenv("IAI_MCP_PROFILE", raising=False)
        assert self._bind("sysadmin") == "sysadmin"
        # Restored: the daemon must not retain one request's scope.
        assert os.environ.get("IAI_MCP_PROFILE") is None

    def test_previous_scope_is_restored_not_dropped(self, monkeypatch):
        monkeypatch.setenv("IAI_MCP_PROFILE", "default")
        assert self._bind("sysadmin") == "sysadmin"
        assert os.environ.get("IAI_MCP_PROFILE") == "default"

    def test_non_string_profile_ignored(self, monkeypatch):
        monkeypatch.setenv("IAI_MCP_PROFILE", "default")
        for bad in (123, ["x"], {"p": 1}, True):
            assert self._bind(bad) == "default"  # type: ignore[arg-type]


class TestClientForwardsScope:
    """The CLI must put the scope on the request — absent means unscoped."""

    def _params_for(self, env_value):
        params = {"session_id": "s"}
        scope = (env_value or "").strip()
        if scope and scope != "all":
            params["profile"] = scope
        return params

    def test_scope_forwarded(self):
        assert self._params_for("sysadmin")["profile"] == "sysadmin"

    def test_all_wildcard_not_forwarded(self):
        # "all" means unscoped; forwarding it would send a literal scope the
        # daemon would then apply.
        assert "profile" not in self._params_for("all")

    def test_unset_not_forwarded(self):
        assert "profile" not in self._params_for(None)

    def test_empty_not_forwarded(self):
        assert "profile" not in self._params_for("")
