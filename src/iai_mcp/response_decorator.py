from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


HELPER_TO_KNOB_ID: dict[str, str] = {
    "_apply_focus_depth": "TUNE-01",
    "_apply_literal_preservation": "TUNE-04",
    "_apply_phrasing_mode": "TUNE-05",
    "_apply_terse_pragmatics": "TUNE-06",
    "_apply_task_support": "TUNE-07",
    "_apply_inertia_awareness": "TUNE-10",
    "_apply_scene_construction": "TUNE-14",
    "sensory_weighting": "TUNE-03",
    "interest_boost": "TUNE-09",
    "wake_depth": "MCP-12",
}


def suggestions_visible(profile: dict, probe_active: bool) -> bool:
    """Single source of truth for whether adjacent suggestions surface.

    Called by both the decorator strip below and the pipeline's recorded
    ``retrieval_used`` flag, so the nightly join can never see a strip
    decision the recorded flag disagrees with.
    """
    mode = profile.get("task_support", "cued_recognition")
    return mode != "blank_recall" or probe_active


def apply_profile(response: dict, profile: dict, *, probe_active: bool = False) -> dict:
    if not isinstance(response, dict) or not isinstance(profile, dict):
        return response

    pre_seeded = response.get("_knobs_applied")
    if isinstance(pre_seeded, dict):
        applied: dict[str, str] = pre_seeded
    else:
        applied = {}

    for helper in (
        _apply_focus_depth,
        _apply_literal_preservation,
        _apply_terse_pragmatics,
        _apply_task_support,
        _apply_scene_construction,
        _apply_sensory_weighting,
        _apply_phrasing_mode,
        _apply_interest_boost,
        _apply_inertia_awareness,
    ):
        helper_raised = False
        try:
            if helper is _apply_task_support:
                helper(response, profile, probe_active)
            else:
                helper(response, profile)
        except Exception as exc:
            logger.debug("profile helper %s failed: %s", helper.__name__, exc)
            helper_raised = True
        if helper_raised:
            continue
        helper_name = helper.__name__
        knob_id = HELPER_TO_KNOB_ID.get(helper_name)
        if knob_id is None:
            continue
        provenance = f"response_decorator.py:{helper_name}"
        if helper_name == "_apply_phrasing_mode":
            mode = profile.get("phrasing_mode", "collaborative")
            if mode == "neutral":
                provenance = f"{provenance}:no-op (mode=neutral)"
        elif helper_name == "_apply_inertia_awareness":
            if not profile.get("inertia_awareness", False):
                provenance = f"{provenance}:no-op (knob=False)"
            elif not response.get("first_turn_recall"):
                provenance = f"{provenance}:no-op (subsequent turn)"
        elif helper_name == "_apply_scene_construction":
            if not profile.get("scene_construction_scaffold", True):
                provenance = f"{provenance}:no-op (knob=False)"
        applied[knob_id] = provenance

    response["_knobs_applied"] = applied
    return response


def _apply_focus_depth(response: dict, profile: dict) -> None:
    try:
        md = profile.get("focus_depth")
        if not isinstance(md, dict) or not md:
            return
        hot_topics = {t for t, depth in md.items() if _as_float(depth, 0.0) > 0.7}
        if not hot_topics:
            return
        hits = response.get("hits")
        if not isinstance(hits, list) or not hits:
            return
        from iai_mcp.core import get_community_names
        names = get_community_names()
        def _key(h):
            if not isinstance(h, dict):
                return 1
            cid = h.get("community_id")
            if cid is None:
                return 1
            name = names.get(str(cid))
            return 0 if name is not None and name in hot_topics else 1
        hits.sort(key=_key)
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_focus_depth: %s", exc)


def _apply_literal_preservation(response: dict, profile: dict) -> None:
    try:
        mode = profile.get("literal_preservation", "strong")
        if mode not in ("strong", "medium", "loose"):
            return
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_literal_preservation: %s", exc)


def _apply_terse_pragmatics(response: dict, profile: dict) -> None:
    try:
        if not profile.get("terse_pragmatics", True):
            return
        filler = (
            "Great question! ",
            "Certainly! ",
            "Of course! ",
        )
        for hit in response.get("hits", []) or []:
            if not isinstance(hit, dict):
                continue
            txt = hit.get("surface_text")
            if isinstance(txt, str):
                for f in filler:
                    if txt.startswith(f):
                        hit["surface_text"] = txt[len(f):]
                        break
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_terse_pragmatics: %s", exc)


def _apply_task_support(response: dict, profile: dict, probe_active: bool = False) -> None:
    try:
        if suggestions_visible(profile, probe_active):
            return
        for hit in response.get("hits", []) or []:
            if isinstance(hit, dict) and "adjacent_suggestions" in hit:
                hit["adjacent_suggestions"] = []
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_task_support: %s", exc)


def _apply_scene_construction(response: dict, profile: dict) -> None:
    try:
        if not profile.get("scene_construction_scaffold", True):
            return
        for hit in response.get("hits", []) or []:
            if not isinstance(hit, dict):
                continue
            hit["_scene_hint"] = {
                "session_id": hit.get("session_id"),
                "captured_at": hit.get("captured_at"),
                "advice": "use as scaffold for autobiographical reconstruction",
            }
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_scene_construction: %s", exc)


def _apply_sensory_weighting(response: dict, profile: dict) -> None:
    try:
        _ = profile.get("sensory_weighting", "neutral")
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_sensory_weighting: %s", exc)


def _apply_phrasing_mode(response: dict, profile: dict) -> None:
    try:
        mode = profile.get("phrasing_mode", "collaborative")
        # ``imperative`` means "leave imperatives alone" and ``neutral`` means
        # "change nothing", so both fall through without touching the response.
        # Only ``collaborative`` rewrites. The schema is
        # enum:collaborative|neutral|imperative -- a branch for any other value
        # could never run, because ``profile_set`` validates on the way in.
        if mode == "neutral":
            return
        if mode == "collaborative":
            substitutions: tuple[tuple[str, str], ...] = (
                ("Try ", "You could try "),
                ("Do ", "Consider "),
                ("Use ", "Try using "),
                ("Run ", "Try running "),
            )
            for hit in response.get("hits", []) or []:
                if not isinstance(hit, dict):
                    continue
                suggestions = hit.get("adjacent_suggestions")
                if not isinstance(suggestions, list):
                    continue
                rewritten: list = []
                for entry in suggestions:
                    if not isinstance(entry, str):
                        rewritten.append(entry)
                        continue
                    new_entry = entry
                    for prefix, replacement in substitutions:
                        if entry.startswith(prefix):
                            new_entry = replacement + entry[len(prefix):]
                            break
                    rewritten.append(new_entry)
                hit["adjacent_suggestions"] = rewritten
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_phrasing_mode: %s", exc)


def _apply_interest_boost(response: dict, profile: dict) -> None:
    try:
        _ = profile.get("interest_boost", 0.0)
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_interest_boost: %s", exc)


def _apply_inertia_awareness(response: dict, profile: dict) -> None:
    try:
        if not profile.get("inertia_awareness", False):
            return
        if not response.get("first_turn_recall"):
            return
        hits = response.get("hits") or []
        if not hits:
            return
        top = hits[0]
        if not isinstance(top, dict):
            return
        literal = top.get("literal_surface")
        if not isinstance(literal, str):
            return
        top["literal_surface"] = f"Resuming from your last session: {literal}"
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("_apply_inertia_awareness: %s", exc)


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
