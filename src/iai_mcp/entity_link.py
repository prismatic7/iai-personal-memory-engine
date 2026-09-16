"""Entity-shared edge mining over lexical postings.

A rare token appearing in a handful of records is an entity-grade anchor
(a name, a place, a project, a distinctive noun). Records sharing such an
anchor get an `entity_shared` edge so the 2-hop spread can cross a
semantic gap similarity cannot bridge: a cue that seeds on one caffeine
mention reaches the sleep-tracker note that shares the anchor even though
the cue itself never says caffeine.

The edge is INFERRED structure: it widens spread reach but is excluded
from the ranking degree — shared vocabulary must not manufacture hubs.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

ENTITY_EDGE_TYPE = "entity_shared"
ENTITY_MIN_DF = 2
ENTITY_MAX_DF = 8
ENTITY_MIN_TOKEN_LEN = 5
ENTITY_MAX_PAIRS_PER_TOKEN = 28
ENTITY_MAX_EDGES_PER_RUN = 500
ENTITY_EDGE_WEIGHT = 0.3


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read an int knob from the environment, falling back to `default`.

    Mirrors the codebase's existing override convention (e.g.
    `hippo/_db.py::_reembed_batch_size`): unset/invalid/out-of-range ->
    default, never an exception on a consolidation path.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


def _max_edges_per_run() -> int:
    """Per-run edge-minting ceiling; `IAI_MCP_ENTITY_MAX_EDGES_PER_RUN`.

    Why this is overridable: the ceiling is a HARD stop, and on a corpus
    larger than a few thousand records it binds long before the corpus is
    exhausted — a measured run stopped at exactly 500 having consumed 38 of
    4,756 eligible tokens. Clustering quality is downstream of graph
    connectivity, and connectivity is downstream of this. Raising it grows
    edges roughly linearly at the same wall-clock cost; 0 disables the
    ceiling entirely (mine every eligible token).
    """
    raw = os.environ.get("IAI_MCP_ENTITY_MAX_EDGES_PER_RUN")
    if raw is not None:
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = ENTITY_MAX_EDGES_PER_RUN
        return val if val >= 0 else ENTITY_MAX_EDGES_PER_RUN
    return ENTITY_MAX_EDGES_PER_RUN


def _max_df() -> int:
    """Upper document-frequency bound; `IAI_MCP_ENTITY_MAX_DF`.

    Tokens shared by MORE than this many records are treated as too common
    to be entity-grade anchors. On a small corpus 8 is sensible; on a large
    one it excludes most genuine mid-frequency anchors (a project name
    mentioned across 20 notes is still a project name).
    """
    return _env_int("IAI_MCP_ENTITY_MAX_DF", ENTITY_MAX_DF)


def _entity_grade(token: str) -> bool:
    return len(token) >= ENTITY_MIN_TOKEN_LEN and token.isalpha()


def mine_entity_edges(
    store: Any,
    *,
    min_df: int = ENTITY_MIN_DF,
    max_df: int | None = None,
    max_pairs_per_token: int = ENTITY_MAX_PAIRS_PER_TOKEN,
    max_edges_per_run: int | None = None,
) -> "dict[str, int]":
    """Mint entity_shared edges from the warm lexical postings.

    Runs on the consolidation side only: ensuring the lexical index may
    pay the O(corpus)-decrypt build, which is never allowed on the awake
    recall path. Edge writes go through boost_edges (canonicalized pair
    keys, merge-insert), so re-running over the same corpus is idempotent.
    """
    try:
        store.lexical_search("entity-link warm-up", k=1)
    except Exception as exc:  # noqa: BLE001 -- no index means nothing to mine
        logger.debug("entity_link lexical warm-up failed: %s", exc)
        return {"tokens_scanned": 0, "tokens_used": 0, "edges_minted": 0}

    idx = getattr(store, "_lexical_idx", None)
    if idx is None:
        return {"tokens_scanned": 0, "tokens_used": 0, "edges_minted": 0}

    # Resolve None -> env-or-default. Explicit caller args still win, so the
    # in-process A/B (passing values directly) is unaffected by the env.
    if max_df is None:
        max_df = _max_df()
    if max_edges_per_run is None:
        max_edges_per_run = _max_edges_per_run()

    postings = idx.iter_token_postings()
    tokens_used = 0
    minted = 0
    pairs_batch: "list[tuple[UUID, UUID]]" = []
    for token, bucket in postings:
        if minted >= max_edges_per_run:
            break
        if not _entity_grade(token):
            continue
        df = len(bucket)
        if df < min_df or df > max_df:
            continue
        rids = sorted(bucket)
        pairs = [
            (rids[i], rids[j])
            for i in range(len(rids))
            for j in range(i + 1, len(rids))
        ][:max_pairs_per_token]
        if not pairs:
            continue
        tokens_used += 1
        for a, b in pairs:
            if minted >= max_edges_per_run:
                break
            try:
                pairs_batch.append((UUID(a), UUID(b)))
            except ValueError:
                continue
            minted += 1

    if pairs_batch:
        store.boost_edges(
            pairs_batch, delta=ENTITY_EDGE_WEIGHT, edge_type=ENTITY_EDGE_TYPE,
        )
        try:
            from iai_mcp.store import flush_edge_buffer
            flush_edge_buffer(store)
        except Exception as exc:  # noqa: BLE001 -- graph builds drain the buffer anyway
            logger.debug("entity_link edge flush deferred: %s", exc)

    result = {
        "tokens_scanned": len(postings),
        "tokens_used": tokens_used,
        "edges_minted": minted,
    }
    logger.info("entity_link %s", result)
    return result
