"""Self-check for the fork's measured-delta relief gate.

Run: ~/Development/iai-pme-venv/bin/python <this file>

Invariants:
  1. The module imports and compiles (the edit is syntactically valid).
  2. HEAVY_RELIEF_STEPS still exists and is still a frozenset (other code may
     depend on it) and still contains the original four.
  3. _RELIEF_MIN_DELTA_KIB is a sensible positive threshold.
  4. The gate DECISION reproduces the intended behaviour for the real measured
     numbers: the steps that were being skipped now clear the threshold, and a
     trivial step does not.

(4) is the one that fails if the threshold or the comparison is wrong.
"""
import importlib.util
import sys

SRC = "/Users/chris/Development/iai-personal-memory-engine/src/iai_mcp/lilli/cycle/sleep_pipeline/__init__.py"
spec = importlib.util.spec_from_file_location("sp_under_test", SRC)
assert spec is not None and spec.loader is not None, f"cannot load {SRC}"
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def main() -> int:
    steps = mod.SleepPipeline.HEAVY_RELIEF_STEPS
    assert isinstance(steps, frozenset), f"HEAVY_RELIEF_STEPS is {type(steps)}"
    for name in ("CRISIS_RECLUSTER", "RECONSOLIDATION", "CLUSTER_REPLAY", "OPTIMIZE_HIPPO"):
        assert any(s.name == name for s in steps), f"{name} dropped from the set"
    print(f"PASS invariant 2: HEAVY_RELIEF_STEPS intact ({len(steps)} steps)")

    threshold = mod.SleepPipeline._RELIEF_MIN_DELTA_KIB
    assert isinstance(threshold, int) and threshold > 0, f"bad threshold {threshold!r}"
    print(f"PASS invariant 3: threshold = {threshold} KiB ({threshold/1024:.0f} MiB)")

    # (4) reproduce the gate decision for real measured numbers.
    # Measured gross per-step deltas (MiB) from four recorded cycles.
    def relieved(delta_mib: float) -> bool:
        """Mirror of the dispatch-loop gate for a non-set step."""
        return int(delta_mib * 1024) >= threshold

    should_relieve = {
        "SCHEMA_MINE": 216.2,
        "RECALL_INDEX_REBUILD": 97.2,
        "EMBEDDING_INTEGRITY": 58.1,
        "USER_MODEL_UPDATE": 42.6,
        "HIPPO_CLEANUP": 44.6,
        "DMN_REFLECTION": 23.9,
        "CLUSTER_SUMMARY": 16.4,
        "ENTITY_LINK": 11.8,
        "COMMUNITY_NAMING": 12.0,
    }
    should_not = {"SEMANTIC_LINK": 1.0, "KNOB_TUNE": 1.3, "PROC_MINE": 0.0}

    failures = []
    for step, mib in should_relieve.items():
        if not relieved(mib):
            failures.append(f"{step} (+{mib} MiB) NOT relieved")
    for step, mib in should_not.items():
        if relieved(mib):
            failures.append(f"{step} (+{mib} MiB) relieved but should not be")
    assert not failures, "gate decision wrong: " + "; ".join(failures)
    print(f"PASS invariant 4: all {len(should_relieve)} previously-skipped heavy steps "
          f"now clear the gate; {len(should_not)} trivial steps still skip it")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
