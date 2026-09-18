"""Retired knob vocabulary still loads onto its knob.

The durable profile blob is written by an older build and read by a newer one
during an upgrade. When the clinical vocabulary was renamed to operational
names, that made every already-persisted blob a *legacy* blob: its keys and
enum members no longer match the registry.

The failure mode is silent, which is why these tests exist. The loader does not
raise on an unrecognised key -- it appends it to ``dropped`` and moves on, so an
unmapped rename resets the user's tuning to defaults with no error anywhere. A
test that only checks "the load succeeded" would pass while the data was gone.

Renaming a persisted key without an alias is therefore a data-loss bug, not a
cosmetic one. These tests pin the alias in both directions: legacy input
migrates, and an unmapped key is still reported as dropped rather than
silently disappearing.
"""

from __future__ import annotations

import pytest

from iai_mcp.lilli.profile.knobs import PROFILE_KNOBS, _validate
from iai_mcp.lilli.profile.persistence import (
    LEGACY_ENUM_ALIASES,
    LEGACY_KNOB_ALIASES,
    _migrate_legacy_knobs,
)


def test_every_alias_targets_a_real_knob() -> None:
    """An alias pointing at a name that does not exist is worse than no alias:
    it makes the legacy value look handled while the loader drops it anyway."""
    for old, new in LEGACY_KNOB_ALIASES.items():
        assert new in PROFILE_KNOBS, f"alias {old!r} -> {new!r} targets no such knob"

    for knob, mapping in LEGACY_ENUM_ALIASES.items():
        assert knob in PROFILE_KNOBS, f"enum aliases for unknown knob {knob!r}"
        spec = PROFILE_KNOBS[knob]
        allowed = spec.value_schema[len("enum:"):].split("|")
        for old, new in mapping.items():
            assert new in allowed, (
                f"enum alias {knob}:{old!r} -> {new!r} produces a value the "
                f"schema rejects ({allowed})"
            )


def test_legacy_knob_names_migrate() -> None:
    raw = {
        "monotropism_depth": {"theatre": 0.8},
        "dunn_quadrant": "seeking",
        "demand_avoidance_tolerance": "collaborative",
        "masking_off": False,
    }
    migrated, renamed = _migrate_legacy_knobs(raw)

    assert set(migrated) == {
        "focus_depth",
        "sensory_weighting",
        "phrasing_mode",
        "terse_pragmatics",
    }
    assert migrated["focus_depth"] == {"theatre": 0.8}
    assert migrated["sensory_weighting"] == "raised"
    assert migrated["terse_pragmatics"] is False
    assert len(renamed) == 5  # 4 knob names + the one enum member


def test_migrated_values_pass_the_current_schema() -> None:
    """The point of migrating is that the result validates; a rename that still
    fails `_validate` would be dropped one step later."""
    raw = {
        "monotropism_depth": {},
        "dunn_quadrant": "low-registration",
        "demand_avoidance_tolerance": "neutral",
        "masking_off": True,
    }
    migrated, _ = _migrate_legacy_knobs(raw)

    for name, value in migrated.items():
        spec = PROFILE_KNOBS[name]
        ok, reason = _validate(spec.value_schema, value)
        assert ok, f"{name}={value!r} failed validation after migration: {reason}"


def test_current_names_pass_through_untouched() -> None:
    raw = {"literal_preservation": "loose", "wake_depth": "deep"}
    migrated, renamed = _migrate_legacy_knobs(raw)

    assert migrated == raw
    assert renamed == []


def test_unknown_keys_are_not_invented_into_the_registry() -> None:
    """A name that is neither current nor aliased must survive the migration
    unchanged so the caller's drop-and-report path still sees it. Quietly
    swallowing it here would hide a genuinely unknown key."""
    migrated, renamed = _migrate_legacy_knobs({"not_a_knob": 1})

    assert migrated == {"not_a_knob": 1}
    assert renamed == []


@pytest.mark.parametrize(
    "legacy, expected",
    [
        ("low-registration", "low"),
        ("seeking", "raised"),
        ("sensitive", "heightened"),
        ("avoiding", "dampened"),
    ],
)
def test_every_retired_enum_member_maps(legacy: str, expected: str) -> None:
    migrated, _ = _migrate_legacy_knobs({"dunn_quadrant": legacy})
    assert migrated["sensory_weighting"] == expected


def test_no_retired_enum_member_survives_in_the_schema() -> None:
    """Guard against a half-done rename: the retired members must not still be
    accepted by the current schema, or the alias would be dead code."""
    spec = PROFILE_KNOBS["sensory_weighting"]
    allowed = spec.value_schema[len("enum:"):].split("|")
    for knob_map in LEGACY_ENUM_ALIASES.values():
        for retired in knob_map:
            assert retired not in allowed, (
                f"{retired!r} is both retired and still accepted by {spec.name}"
            )
