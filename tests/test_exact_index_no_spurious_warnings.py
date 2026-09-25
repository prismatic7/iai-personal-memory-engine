"""Self-check for the exact-index warning suppression.

Run:  ~/Development/iai-pme-venv/bin/python <this file>
Exits non-zero if the spurious warnings return or if results changed.

Two invariants:
  1. top_k emits NO RuntimeWarning for clean data (the fix).
  2. top_k still returns the SAME scores as a non-BLAS computation
     (errstate must not alter arithmetic).
"""
import importlib.util
import sys
import warnings

import numpy as np

SRC = "/Users/chris/Development/iai-personal-memory-engine/src/iai_mcp/store/_exact_index.py"

spec = importlib.util.spec_from_file_location("exi_under_test", SRC)
assert spec is not None and spec.loader is not None, f"cannot load {SRC}"
exi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exi)

F32 = np.float32
D = 384
rng = np.random.default_rng(0)


def unit_vec():
    v = rng.normal(0, 1, D).astype(F32)
    return v / np.linalg.norm(v)


def main() -> int:
    rows = [(f"r{i}", unit_vec().tobytes()) for i in range(50)]
    idx = exi.ExactCosineIndex(embed_dim=D)
    assert idx.build(rows), "build() refused"
    assert idx._warm and idx._count == 50, "index did not warm"

    cue = unit_vec()

    # --- invariant 1: no warnings ---
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        out = idx.top_k(cue, k=5)
    runtime = [x for x in w if issubclass(x.category, RuntimeWarning)]
    assert not runtime, f"spurious RuntimeWarnings returned: {[str(x.message) for x in runtime]}"
    print("PASS invariant 1: no RuntimeWarning from top_k")

    # --- invariant 2: scores unchanged (errstate is warning-only) ---
    m = idx._m[: idx._count]
    expected = m @ cue                      # may warn; that is fine here
    got = {rid: s for rid, s in out}
    for i, score in enumerate(expected):
        rid = idx._ids[i]
        if rid in got:
            assert abs(got[rid] - float(score)) < 1e-6, (
                f"score drift for {rid}: {got[rid]} vs {float(score)}"
            )
    print("PASS invariant 2: top_k scores match the raw matmul")

    # --- invariant 3: the guard cases still behave ---
    idx2 = exi.ExactCosineIndex(embed_dim=D)
    idx2.build([("zero", np.zeros(D, dtype=F32).tobytes()),
                ("inf", np.full(D, np.inf, dtype=F32).tobytes()),
                ("nan", np.full(D, np.nan, dtype=F32).tobytes()),
                ("ok", unit_vec().tobytes())])
    mm = idx2._m[: idx2._count]
    assert np.isfinite(mm).all(), "build() let non-finite into _m"
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        idx2.top_k(np.full(D, np.nan, dtype=F32), k=4)
    assert not [x for x in w if issubclass(x.category, RuntimeWarning)], \
        "adversarial input still warns"
    print("PASS invariant 3: adversarial rows/cues coerced cleanly, no warnings")

    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
