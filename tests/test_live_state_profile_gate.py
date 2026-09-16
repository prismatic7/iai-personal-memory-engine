"""The live-state render must not hand one profile's goal to another.

`render_live_state_segment` reads the process-GLOBAL focal task, so the owner
it finds may belong to a different Hermes profile. The existing session-owner
gate downstream compares session **ids** and cannot catch that — a profile
boundary crosses sessions by construction — so another profile's goal would
arrive wearing "your goal" framing.

These tests stub `working_tier.read_task` rather than touching a store: the
render only needs an entry object, and the assertion is about the gate, not
about retrieval.
"""

from __future__ import annotations

import pytest

from iai_mcp import session as session_mod


class _Entry:
    def __init__(self, session_id, goal="A_GOAL"):
        self.session_id = session_id
        self.goal = goal
        self.focus = None
        self.next_action = None


@pytest.fixture
def stub_focal(monkeypatch):
    """Replace working_tier.read_task with a stub returning a chosen entry."""
    import iai_mcp.working_tier as wt

    def _set(entry):
        monkeypatch.setattr(wt, "read_task", lambda **kw: entry)

    return _set


@pytest.fixture
def profile_map(monkeypatch):
    """Pin the session→profile map so no real ~/.hermes is read."""

    def _set(mapping):
        monkeypatch.setattr(
            session_mod, "_profile_gate_for", None, raising=False
        )
        import iai_mcp.profile_scope as ps

        monkeypatch.setattr(ps, "session_profile_of", lambda sid: mapping.get(sid))

    return _set


class TestLiveStateProfileGate:
    def test_other_profiles_goal_is_suppressed(self, monkeypatch, stub_focal, profile_map):
        monkeypatch.setenv("IAI_MCP_PROFILE", "default")
        profile_map({"s-sys": "sysadmin"})
        stub_focal(_Entry("s-sys"))
        assert session_mod.render_live_state_segment() == ""

    def test_own_profiles_goal_is_emitted(self, monkeypatch, stub_focal, profile_map):
        monkeypatch.setenv("IAI_MCP_PROFILE", "default")
        profile_map({"s-def": "default"})
        stub_focal(_Entry("s-def"))
        out = session_mod.render_live_state_segment()
        assert "A_GOAL" in out
        assert "session: s-def" in out

    def test_unknown_owner_keeps_existing_behaviour(self, monkeypatch, stub_focal, profile_map):
        # A cron id or a pruned session is not in the map. Suppressing on
        # unknown would blank the block for every legitimate case the map
        # cannot see, so unknown must render (the session-owner gate handles
        # those downstream).
        monkeypatch.setenv("IAI_MCP_PROFILE", "default")
        profile_map({})
        stub_focal(_Entry("cron_job_123"))
        assert "A_GOAL" in session_mod.render_live_state_segment()

    def test_all_wildcard_disables_the_gate(self, monkeypatch, stub_focal, profile_map):
        monkeypatch.setenv("IAI_MCP_PROFILE", "all")
        profile_map({"s-sys": "sysadmin"})
        stub_focal(_Entry("s-sys"))
        assert "A_GOAL" in session_mod.render_live_state_segment()

    def test_unset_scope_disables_the_gate(self, monkeypatch, stub_focal, profile_map):
        monkeypatch.delenv("IAI_MCP_PROFILE", raising=False)
        profile_map({"s-sys": "sysadmin"})
        stub_focal(_Entry("s-sys"))
        assert "A_GOAL" in session_mod.render_live_state_segment()

    def test_attribution_failure_does_not_suppress(self, monkeypatch, stub_focal):
        # If the map cannot be built, the render must still work: a broken
        # attribution layer must never blank every block.
        import iai_mcp.profile_scope as ps

        monkeypatch.setenv("IAI_MCP_PROFILE", "default")

        def _boom(_sid):
            raise RuntimeError("map unavailable")

        monkeypatch.setattr(ps, "session_profile_of", _boom)
        stub_focal(_Entry("s-anything"))
        assert "A_GOAL" in session_mod.render_live_state_segment()
