from __future__ import annotations


from iai_mcp.profile import (
    LIVE_KNOB_NAMES,
    DEFERRED_KNOB_NAMES,
    PROFILE_KNOBS,
    default_state,
    profile_get,
    profile_set,
)


def test_live_knob_names_has_10_knobs():
    assert len(LIVE_KNOB_NAMES) == 10


def test_deferred_knob_names_empty():
    assert DEFERRED_KNOB_NAMES == frozenset()


def test_all_requirement_ids_present():
    profile_specs = [
        s for s in PROFILE_KNOBS.values() if s.requirement_id.startswith("TUNE-")
    ]
    assert len(profile_specs) == 9
    req_ids = {spec.requirement_id for spec in profile_specs}
    expected = {
        "TUNE-01", "TUNE-03", "TUNE-04", "TUNE-05",
        "TUNE-06", "TUNE-07", "TUNE-09", "TUNE-10",
        "TUNE-14",
    }
    assert req_ids == expected
    assert len(PROFILE_KNOBS) == 10
    assert "wake_depth" in PROFILE_KNOBS
    assert PROFILE_KNOBS["wake_depth"].requirement_id == "MCP-12"


def test_focus_depth_live_accepts_dict():
    state = default_state()
    r = profile_set(
        "focus_depth",
        {"coding": 0.8, "gardening": 0.3},
        state,
    )
    assert r["status"] == "ok"
    assert state["focus_depth"] == {"coding": 0.8, "gardening": 0.3}


def test_focus_depth_live_rejects_out_of_range():
    state = default_state()
    r = profile_set("focus_depth", {"x": 1.5}, state)
    assert r["status"] == "error"


def test_focus_depth_live_rejects_non_dict():
    state = default_state()
    r = profile_set("focus_depth", 3, state)
    assert r["status"] == "error"


def test_sensory_weighting_live():
    state = default_state()
    r = profile_set("sensory_weighting", "raised", state)
    assert r["status"] == "ok"
    assert state["sensory_weighting"] == "raised"


def test_sensory_weighting_rejects_garbage():
    state = default_state()
    r = profile_set("sensory_weighting", "garbage", state)
    assert r["status"] == "error"


def test_phrasing_mode_live():
    state = default_state()
    for value in ("collaborative", "neutral", "imperative"):
        r = profile_set("phrasing_mode", value, state)
        assert r["status"] == "ok", f"expected {value} accepted"
    assert state["phrasing_mode"] == "imperative"


def test_inertia_awareness_live():
    state = default_state()
    r_ok = profile_set("inertia_awareness", True, state)
    assert r_ok["status"] == "ok"
    r_bad = profile_set("inertia_awareness", 1, state)
    assert r_bad["status"] == "error"


def test_interest_boost_live():
    state = default_state()
    r_ok = profile_set("interest_boost", 0.75, state)
    assert r_ok["status"] == "ok"
    r_bad = profile_set("interest_boost", 2.0, state)
    assert r_bad["status"] == "error"


def test_HIPPEA_precision_spec_added_wire_to_mem_03():
    if "HIPPEA_precision" in PROFILE_KNOBS:
        spec = PROFILE_KNOBS["HIPPEA_precision"]
        assert "float_range:" in spec.value_schema
    else:
        spec = PROFILE_KNOBS["sensory_weighting"]
        assert spec.value_schema.startswith("enum:")


def test_profile_get_returns_10_live_entries():
    state = default_state()
    result = profile_get(None, state)
    assert len(result["live"]) == 10
    assert len(result["deferred"]) == 0


def test_profile_get_focus_depth_returns_default_dict():
    state = default_state()
    r = profile_get("focus_depth", state)
    assert r["knob"] == "focus_depth"
    assert "value" in r
    assert isinstance(r["value"], dict)


def test_default_state_returns_independent_mutable_defaults():
    s1 = default_state()
    s2 = default_state()

    assert s1["focus_depth"] is not s2["focus_depth"]

    s1["focus_depth"]["coding"] = 0.9

    assert s2["focus_depth"] == {}
    assert PROFILE_KNOBS["focus_depth"].default == {}
