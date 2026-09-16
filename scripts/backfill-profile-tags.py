#!/usr/bin/env python3
"""Backfill `profile:<name>` tags onto records captured before profile tagging.

WHY
    The iai store is shared by every Hermes profile on the machine. Records
    captured before this fork were never tagged with their originating profile,
    so a profile-scoped recall cannot attribute them. Provenance DOES carry the
    originating ``session_id``, and Hermes records which profile owns each
    session (each profile's ``state.db.messages``), so the attribution is
    recoverable exactly — no guessing.

WHAT IT DOES
    For each record with no ``profile:*`` tag:
      1. decrypt ``provenance_json`` (AES-GCM, AAD = lowercased record id),
      2. read ``session_id``,
      3. map it to a profile via ``profile_scope.build_session_profile_map()``,
      4. add ``profile:<name>`` to that record's tags.

    Records whose session cannot be mapped are left alone (unattributed is a
    real answer — never invent one).

SAFETY
    * Idempotent: already-tagged records are skipped, so a re-run is a no-op.
    * ``--dry-run`` is the DEFAULT. Pass ``--apply`` to write.
    * Requires the daemon STOPPED — the store takes an exclusive lock, and
      writing while the daemon serves it would race the writer.
    * Takes a store snapshot first (``--apply`` only).

USAGE
    iai-mcp daemon stop
    <venv>/bin/python scripts/backfill-profile-tags.py            # dry run
    <venv>/bin/python scripts/backfill-profile-tags.py --apply
    iai-mcp daemon start
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Allow running from a source checkout without installing.
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill profile:<name> tags onto pre-tagging records.")
    ap.add_argument("--apply", action="store_true", help="write the tags (default: dry run)")
    ap.add_argument("--store", default=os.environ.get("IAI_MCP_STORE") or str(Path.home() / ".iai-mcp"))
    ap.add_argument("--limit", type=int, default=0, help="process at most N records (0 = all)")
    args = ap.parse_args()

    store_root = Path(args.store).expanduser()
    hippo = store_root / "hippo"
    if not (hippo / "brain.sqlite3").is_file():
        print(f"ERROR: no store at {hippo}", file=sys.stderr)
        return 2

    from iai_mcp.profile_scope import build_session_profile_map

    # ── 1. session → profile ────────────────────────────────────────────────
    session_profile = build_session_profile_map()
    print(f"session→profile map: {len(session_profile)} sessions "
          f"({dict(Counter(session_profile.values()))})")
    if not session_profile:
        print("ERROR: empty session→profile map — nothing to attribute against.", file=sys.stderr)
        return 2

    # ── 2. open the store (exclusive; daemon must be stopped) ───────────────
    from iai_mcp.store import MemoryStore
    try:
        store = MemoryStore(path=str(store_root))
    except Exception as exc:
        print(f"ERROR: cannot open store ({exc}).\n"
              f"       Is the daemon running? Stop it first: iai-mcp daemon stop",
              file=sys.stderr)
        return 2

    try:
        records = store.all_records()
        print(f"records: {len(records)}")

        # ── 3. snapshot before writing ──────────────────────────────────────
        if args.apply:
            snap = store_root / f"hippo.backfill-{int(time.time())}"
            print(f"snapshotting store → {snap}")
            shutil.copytree(hippo, snap)
            print("snapshot done")

        todo: list[tuple[Any, str]] = []
        already = 0
        unattributed = 0
        for rec in records:
            if args.limit and len(todo) >= args.limit:
                break
            tags = rec.tags or []
            if any(t.startswith("profile:") for t in tags):
                already += 1
                continue
            sid = None
            try:
                prov = rec.provenance or []
                if prov and isinstance(prov[0], dict):
                    sid = prov[0].get("session_id")
            except Exception:  # noqa: BLE001
                sid = None
            prof = session_profile.get(str(sid)) if sid else None
            if prof:
                todo.append((rec.id, prof))
            else:
                unattributed += 1

        print(f"  already tagged : {already}")
        print(f"  attributable   : {len(todo)}")
        print(f"  unattributable : {unattributed}  (left alone)")
        print(f"  by profile     : {dict(Counter(p for _, p in todo))}")

        if not todo:
            print("nothing to do.")
            return 0
        if not args.apply:
            print("\nDRY RUN — no changes written. Re-run with --apply.")
            return 0

        # ── 4. write ────────────────────────────────────────────────────────
        written = 0
        failed = 0
        for rid, prof in todo:
            try:
                if store.add_tags(rid, [f"profile:{prof}"]):
                    written += 1
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 — one bad row must not abort the pass
                failed += 1
                print(f"  ! {rid}: {type(exc).__name__}: {exc}", file=sys.stderr)

        print(f"\nwrote {written} tags ({failed} failed)")
        return 0 if failed == 0 else 1
    finally:
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
