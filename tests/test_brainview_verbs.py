"""BrainView verb behaviour, exercised directly.

These tests used to drive the local HTTP dashboard. That dashboard has been
removed, but the verbs it served are still live: the daemon relays them over
its socket for ``iai-mcp teach`` / ``search``, and they are the mutation-capable
surface (capture, forget hint, pin, rescue).

So the transport tests are gone and the BEHAVIOURAL invariants are kept, called
straight through ``BrainView`` instead of over urllib. The invariants are the
point — a hint must never delete, a pin must refuse the hint, capture must go
through the dedup-gated spine — and none of them depended on HTTP.
"""

from __future__ import annotations

import threading
from uuid import UUID

import pytest

from iai_mcp.brainview import BrainView
from iai_mcp.capture import capture_turn
from iai_mcp.store import MemoryStore, flush_record_buffer


@pytest.fixture(params=["stdlib", "lilli"])
def driver(request, tmp_path, monkeypatch):
    if request.param == "lilli":
        try:
            import iai_mcp_native  # noqa: F401, PLC0415
        except ImportError:
            pytest.skip("iai_mcp_native not built")
        monkeypatch.setenv("LILLI_STORAGE_DRIVER", "lilli")
    else:
        monkeypatch.delenv("LILLI_STORAGE_DRIVER", raising=False)
    return request.param


def _seed(store: MemoryStore, text: str, session: str = "s") -> str:
    result = capture_turn(
        store, cue="", text=text, tier="episodic", session_id=session, role="user",
    )
    assert result["status"] == "inserted", result
    flush_record_buffer(store)
    return result["record_id"]


@pytest.fixture
def view_and_store(driver, tmp_path):
    store = MemoryStore(path=tmp_path)
    return BrainView(store), store


def test_capture_dedups_by_content_not_by_copy(view_and_store):
    """Capture is content-bound: the same note reinforces the existing record
    rather than storing a second copy (source_uuid is the text hash)."""
    view, store = view_and_store
    text = "A dedup-gated capture."

    first = view.capture_direct(text)
    assert first.get("status") in {"inserted", "duplicate"}, first

    second = view.capture_direct(text)
    assert second.get("status") == "reinforced", (
        f"a re-capture of identical text must reinforce, not insert: {second}"
    )
    assert second.get("record_id") == first.get("record_id"), (
        "the reinforcement must land on the SAME record"
    )

    surfaces = [r.literal_surface for r in store.iter_records()]
    assert surfaces.count(text) == 1, (
        f"identical text must not produce two records; got {surfaces.count(text)}"
    )


def test_forget_hint_feeds_decay_never_deletes(view_and_store):
    view, store = view_and_store
    text = "An obsolete fact the user wants to fade out."
    rid = _seed(store, text)

    result = view.forget_hint(rid)
    assert result["status"] == "queued_for_forgetting", result

    rec = store.get(UUID(rid))
    assert rec is not None, "hint must NEVER delete the record"
    assert rec.literal_surface == text
    assert float(rec.centrality or 0.0) == 0.0
    assert rec.never_decay is False
    assert any(p.get("cue") == "user-forget-hint" for p in (rec.provenance or [])), (
        "the hint must be traceable in provenance"
    )


def test_forget_hint_refuses_pinned(view_and_store):
    view, store = view_and_store
    rid = _seed(store, "A pinned foundational fact.")
    from iai_mcp.store import RECORDS_TABLE

    store.db.open_table(RECORDS_TABLE).update(
        where=f"id = '{rid}'", values={"pinned": True},
    )

    result = view.forget_hint(rid)
    assert result["status"] == "refused", result
    rec = store.get(UUID(rid))
    assert rec is not None and rec.pinned


def test_pin_toggle_protects_and_releases(view_and_store):
    """Pin protects (erasure gate + lossy merges refuse); unpin releases; both
    leave a provenance trace."""
    view, store = view_and_store
    rid = _seed(store, "A fact worth protecting forever.")

    res = view.pin(rid, True)
    assert res["status"] == "pinned", res
    rec = store.get(UUID(rid))
    assert rec.pinned and rec.never_merge
    assert any(p.get("cue") == "user-pin" for p in rec.provenance or [])
    assert view.forget_hint(rid)["status"] == "refused", "pin must block the hint"

    res = view.pin(rid, False)
    assert res["status"] == "unpinned", res
    rec = store.get(UUID(rid))
    assert not rec.pinned and not rec.never_merge


def test_surface_returns_full_decrypted_text(view_and_store):
    view, store = view_and_store
    long_text = ("Verbatim recall is guaranteed. " * 30).strip()
    rid = _seed(store, long_text)

    res = view.surface(rid)
    assert res["surface"] == long_text, "full text, not the excerpt"


def test_overview_counts_seeded_records(view_and_store):
    view, store = view_and_store
    _seed(store, "The brain watches itself think.")

    overview = view.overview()
    assert overview["counts"]["episodic"] == 1
    assert "lifecycle" in overview


def test_rescue_cancels_fading_and_untombstones(view_and_store):
    view, store = view_and_store
    rid = _seed(store, "A fact that was fading but is wanted again.")

    assert view.forget_hint(rid)["status"] == "queued_for_forgetting"
    res = view.rescue(rid)
    assert res["status"] in {"rescued", "restored", "ok"}, res

    rec = store.get(UUID(rid))
    assert rec is not None
    assert float(rec.centrality or 0.0) > 0.0, "rescue must restore centrality"


def test_browse_folders_reflect_the_brains_own_groupings(view_and_store):
    view, store = view_and_store
    _seed(store, "A record to group.")

    res = view.browse(None)
    assert isinstance(res, dict)


def test_browse_rejects_non_dict_folder(view_and_store):
    view, _store = view_and_store
    res = view.browse("not-a-dict")  # type: ignore[arg-type]
    assert isinstance(res, dict)


def test_search_finds_memory_semantically(view_and_store):
    view, store = view_and_store
    _seed(store, "The sound designer calibrated the reverb tail in QLab.")

    res = view.search("reverb calibration", k=5)
    assert isinstance(res, dict)
