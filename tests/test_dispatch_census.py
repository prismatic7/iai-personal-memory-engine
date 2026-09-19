"""The RPC dispatch census.

Counts which verbs anything actually calls, so redundant surfaces can be
identified from usage rather than from reading code. Off unless
``IAI_MCP_DISPATCH_CENSUS=1``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iai_mcp import core


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    """Point daemon_state at a throwaway file so no test touches the real one."""
    from iai_mcp import daemon_state

    target = tmp_path / ".daemon-state.json"
    monkeypatch.setattr(daemon_state, "STATE_PATH", target)
    monkeypatch.setattr(daemon_state, "load_state", lambda: _load(target))
    monkeypatch.setattr(daemon_state, "save_state", lambda s: _save(target, s))
    return target


def _load(target: Path) -> dict:
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(target: Path, state: dict) -> None:
    target.write_text(json.dumps(state), encoding="utf-8")


def test_census_is_inert_when_disabled(state_file, monkeypatch):
    monkeypatch.delenv("IAI_MCP_DISPATCH_CENSUS", raising=False)
    core._census_dispatch("memory_recall")
    core._census_dispatch("memory_recall")
    assert not state_file.exists(), "no state must be written when the gate is off"


def test_census_counts_each_method_separately(state_file, monkeypatch):
    monkeypatch.setenv("IAI_MCP_DISPATCH_CENSUS", "1")
    for _ in range(3):
        core._census_dispatch("memory_recall")
    core._census_dispatch("memory_capture")

    counts = _load(state_file)["rpc_dispatch"]
    assert counts == {"memory_recall": 3, "memory_capture": 1}


def test_census_accumulates_across_calls(state_file, monkeypatch):
    """A restart or a second writer must add to the tally, not reset it."""
    monkeypatch.setenv("IAI_MCP_DISPATCH_CENSUS", "1")
    core._census_dispatch("memory_recall")
    core._census_dispatch("memory_recall")
    assert _load(state_file)["rpc_dispatch"] == {"memory_recall": 2}


def test_census_replaces_a_corrupt_counter(state_file, monkeypatch):
    """A non-dict value at the key must not raise -- the census is advisory."""
    monkeypatch.setenv("IAI_MCP_DISPATCH_CENSUS", "1")
    _save(state_file, {"rpc_dispatch": "garbage"})
    core._census_dispatch("memory_recall")
    assert _load(state_file)["rpc_dispatch"] == {"memory_recall": 1}


def test_census_never_raises_even_when_state_write_fails(monkeypatch):
    """A measurement instrument must not break the path it measures."""
    monkeypatch.setenv("IAI_MCP_DISPATCH_CENSUS", "1")
    from iai_mcp import daemon_state

    def _boom(_state):
        raise OSError("disk full")

    monkeypatch.setattr(daemon_state, "update_state", _boom)
    core._census_dispatch("memory_recall")  # must not raise


def test_dispatch_counts_before_doing_work(state_file, monkeypatch):
    """The count must happen on an unknown method too -- the call was made,
    even though dispatch then raises."""
    monkeypatch.setenv("IAI_MCP_DISPATCH_CENSUS", "1")
    with pytest.raises(core.UnknownMethodError):
        core.dispatch(None, "definitely_not_a_method", {})  # type: ignore[arg-type]
    assert _load(state_file)["rpc_dispatch"] == {"definitely_not_a_method": 1}
