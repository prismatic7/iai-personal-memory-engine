"""Semantic (embedding kNN) edge mining — the second edge source.

`entity_link` connects records that share a rare LITERAL token. Most records
share no such token, so a large part of the corpus stays edgeless and Leiden
has nothing to cluster on (measured on this store: 99.8% singleton
communities). This module adds the reach lexical matching cannot have, by
linking records whose stored VECTORS are close.

Design decisions, each with the evidence behind it:

* **Vectors come from the store's in-process exact index, not the DB column.**
  There are two vectors in this store and they are NOT interchangeable:
  `records.embedding` is 1536-dim, the ANN index is 384-dim, and feeding the
  column to the index raises `vector length 1536 != index dim 384`. The
  in-process index (`MemoryStore._exact_index`) already holds the exact 384-dim
  vectors the recall path serves, so we read from there — no reload, no
  dimension filter, and it is the same neighbourhood the house already trusts.

* **A similarity floor is mandatory.** Linking every record to its nearest
  neighbour regardless of distance wires the corpus into one blob and destroys
  community structure instead of revealing it. Full-corpus calibration
  (nearest-neighbour cosine similarity, self excluded): p50 = 0.862,
  p90 = 1.000, min 0.517. The default 0.80 sits below the median so the
  common case contributes, and well above the floor of the distribution so
  loose pairs do not.

* **Bounded per run.** `max_edges_per_run` caps each pass, mirroring
  `entity_link.ENTITY_MAX_EDGES_PER_RUN`, so a first pass over a mature corpus
  cannot flood the edges table inside one cycle budget. Sweepable via
  `IAI_MCP_SEMANTIC_MAX_EDGES_PER_RUN`.

* **Idempotent.** Writes go through `boost_edges`, which canonicalises pair
  keys and merge-inserts, so re-running over the same corpus adds no duplicate
  rows.

* **Off by default.** `IAI_MCP_SEMANTIC_EDGES_ON=1` enables the pipeline step.
  A new write source on a live store should be an explicit operator decision,
  and the sweep showed the floor materially changes graph shape — that is not
  something to switch on silently.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

SEMANTIC_EDGE_TYPE = "semantic_knn"

#: Default minimum cosine similarity to accept an edge. See the module
#: docstring for the calibration this follows from.
SEMANTIC_MIN_SIMILARITY_DEFAULT = 0.80

#: Default neighbours examined per record, beyond the self-hit.
SEMANTIC_K_DEFAULT = 5

#: Default per-run edge ceiling. Larger than entity_link's 500 because the
#: semantic neighbourhood is far denser (measured: 34,672 pairs at floor 0.85
#: over 12,468 records), so 500 would bind almost immediately.
SEMANTIC_MAX_EDGES_PER_RUN_DEFAULT = 20_000

#: Edge weight. Matches entity_link's inferred-structure weight so neither
#: source dominates the other in the edges table.
SEMANTIC_EDGE_WEIGHT = 0.3


def _env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if lo <= val <= hi else default


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


def min_similarity() -> float:
    """Floor knob; `IAI_MCP_SEMANTIC_MIN_SIMILARITY` (clamped to [0, 1])."""
    return _env_float(
        "IAI_MCP_SEMANTIC_MIN_SIMILARITY",
        SEMANTIC_MIN_SIMILARITY_DEFAULT,
        lo=0.0,
        hi=1.0,
    )


def neighbours_per_record() -> int:
    """k knob; `IAI_MCP_SEMANTIC_K`."""
    return max(1, _env_int("IAI_MCP_SEMANTIC_K", SEMANTIC_K_DEFAULT, minimum=1))


def max_edges_per_run() -> int:
    """Per-run ceiling; `IAI_MCP_SEMANTIC_MAX_EDGES_PER_RUN` (0 = no ceiling)."""
    return _env_int(
        "IAI_MCP_SEMANTIC_MAX_EDGES_PER_RUN",
        SEMANTIC_MAX_EDGES_PER_RUN_DEFAULT,
        minimum=0,
    )


def semantic_edges_enabled() -> bool:
    """Whether the pipeline step should run; `IAI_MCP_SEMANTIC_EDGES_ON=1`."""
    return os.environ.get("IAI_MCP_SEMANTIC_EDGES_ON") == "1"


def mine_semantic_edges(
    store: Any,
    *,
    k: int | None = None,
    floor: float | None = None,
    max_edges: int | None = None,
) -> dict[str, int | str]:
    """Mint `semantic_knn` edges from the store's in-process vector index.

    Reads the resident exact index (built for the awake recall path); a cold
    index is warmed via the store's own single-flight build rather than
    loading a second copy. Never raises: an enrichment step must not fail the
    cycle, so a failure is reported in the returned dict.

    Returns counts only — no ids, no addresses, no vectors (the house's log
    discipline: event names and counts).
    """
    if k is None:
        k = neighbours_per_record()
    if floor is None:
        floor = min_similarity()
    if max_edges is None:
        max_edges = max_edges_per_run()

    try:
        idx = getattr(store, "_exact_index", None)
        if idx is None:
            return {"semantic_edges": 0, "semantic_error": "no_exact_index"}

        if not idx.is_warm:                      # @property, not a method
            # The store owns the single-flight cold build; calling it keeps
            # one corpus scan in the process rather than two racing.
            build = getattr(store, "_build_exact_index_sync", None)
            if callable(build):
                build()
        if not idx.is_warm:                      # @property
            return {"semantic_edges": 0, "semantic_error": "index_cold"}

        rows = idx.snapshot_rows()
        if rows is None:
            return {"semantic_edges": 0, "semantic_error": "snapshot_unavailable"}

        pairs: list[tuple[str, str]] = []
        examined = 0
        below_floor = 0
        capped = False

        for record_id, vec in rows:
            if max_edges and len(pairs) >= max_edges:
                capped = True
                break
            hits = idx.top_k(vec, k + 1)
            if not hits:
                continue
            for other_id, sim in hits:
                if other_id == record_id:
                    continue                 # self-hit
                examined += 1
                if sim < floor:
                    below_floor += 1
                    continue
                pairs.append((record_id, other_id))
                if max_edges and len(pairs) >= max_edges:
                    capped = True
                    break

        if not pairs:
            return {
                "semantic_edges": 0,
                "semantic_examined": examined,
                "semantic_below_floor": below_floor,
            }

        written = store.boost_edges(pairs, delta=SEMANTIC_EDGE_WEIGHT,
                                    edge_type=SEMANTIC_EDGE_TYPE)
        return {
            "semantic_candidates": len(pairs),
            "semantic_examined": examined,
            "semantic_below_floor": below_floor,
            "semantic_edges": len(written),
            "semantic_capped": 1 if capped else 0,
        }
    except Exception as exc:  # noqa: BLE001 -- enrichment must never fail a cycle
        logger.warning("semantic edge mining degraded: %s", exc)
        return {"semantic_edges": 0, "semantic_error": type(exc).__name__}
