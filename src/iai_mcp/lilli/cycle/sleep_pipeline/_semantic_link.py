from __future__ import annotations

import logging
import os
from typing import Any, Callable

from iai_mcp.lilli.cycle.sleep_pipeline import SleepStep

logger = logging.getLogger(__name__)


def step_semantic_link(
    self, interrupt_check: Callable[[], bool] | None,
) -> tuple[bool, dict[str, Any]]:
    """Mint embedding-kNN edges, alongside the lexical `entity_link` step.

    Gated OFF by default (`IAI_MCP_SEMANTIC_EDGES_ON=1` to enable). This is a
    NEW write source on a live graph and the floor materially changes graph
    shape, so enabling it is an explicit operator decision rather than a
    silent default.

    Runs AFTER ENTITY_LINK in the step order: lexical edges are cheap and
    deterministic, so they land first and the semantic pass fills the reach
    lexical matching structurally cannot (records sharing no rare token).

    Never fails the cycle — a degraded enrichment returns a payload carrying
    the error name, following `step_entity_link`.
    """
    if not os.environ.get("IAI_MCP_SEMANTIC_EDGES_ON") == "1":
        return True, {"semantic_link": "disabled"}
    if self._check_interrupt(SleepStep.SEMANTIC_LINK, 0, interrupt_check):
        return False, {}

    from iai_mcp.semantic_edges import mine_semantic_edges

    try:
        result = mine_semantic_edges(self._store)
    except Exception as exc:  # noqa: BLE001 -- enrichment must never fail a cycle
        logger.warning("semantic_link step degraded: %s", exc)
        return True, {"semantic_link_error": type(exc).__name__}
    return True, result
