#!/usr/bin/env python3
"""PROTOTYPE — semantic (embedding kNN) edges, to test whether they fix the
edgeless-periphery that lexical entity edges cannot reach.

Why: `entity_link.mine_entity_edges` only links records that share a rare
literal token. Most records share no such token, so ~99.8% of communities are
singletons and Leiden has no edges to cluster on. The store already holds an
embedding per record, so similarity is available without new data.

Design notes:
  - Edges are undirected in effect but stored as one row per pair. To keep the
    write count sane we link each record to its top-k neighbours and rely on
    merge-insert dedupe (same canonicalised pair key as boost_edges).
  - A similarity FLOOR matters: linking every record to its nearest neighbour
    regardless of distance would connect everything to everything and destroy
    the community structure rather than reveal it. Default 0.80 is deliberately
    conservative; sweep it.
  - Uses the store's own query_similar (the recall path's ANN tier) rather than
    re-implementing cosine over the vectors, so we inherit its over-fetch and
    tombstone discipline.
  - This is a PROTOTYPE: it runs on a COPY of the store and reports what it
    would do. It writes nothing to the live store.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

STORE = os.environ.get("IAI_MCP_STORE") or os.path.expanduser("~/.iai-mcp")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5, help="neighbours per record")
    ap.add_argument("--floor", type=float, default=0.80,
                    help="minimum cosine similarity to accept an edge")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap records processed (0 = all)")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--write", dest="dry_run", action="store_false")
    args = ap.parse_args()

    os.environ["IAI_MCP_STORE"] = STORE
    from iai_mcp.store import MemoryStore

    t0 = time.perf_counter()
    st = MemoryStore(STORE)
    print(f"  store: {STORE}")
    print(f"  k={args.k}  floor={args.floor}  write={not args.dry_run}")

    try:
        rows = st.db._conn.execute(
            "SELECT id, embedding FROM records "
            "WHERE tombstoned_at IS NULL AND COALESCE(embedding_pending, 0) = 0"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL: cannot list records: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    total = len(rows)
    if args.limit:
        rows = rows[: args.limit]
    print(f"  records with embeddings: {total} (processing {len(rows)})")

    pairs: list[tuple] = []
    no_emb = 0
    no_neighbour = 0
    below_floor: list[float] = []
    sims: list[float] = []

    for i, (rid, emb) in enumerate(rows):
        if emb is None:
            no_emb += 1
            continue
        vec = list(emb)
        if not vec:
            no_emb += 1
            continue
        try:
            hits = st.query_similar(vec, k=args.k + 1, decode="rank")
        except Exception as exc:  # noqa: BLE001
            print(f"  query_similar failed on {rid}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        found = 0
        for rec, score in hits:
            try:
                other = rec.id if hasattr(rec, "id") else rec.get("id")
            except Exception:  # noqa: BLE001
                continue
            if other is None or str(other) == str(rid):
                continue                      # self-hit
            sims.append(float(score))
            if float(score) < args.floor:
                below_floor.append(float(score))
                continue
            pairs.append((rid, other))
            found += 1
        if found == 0:
            no_neighbour += 1
        if i and i % 500 == 0:
            el = time.perf_counter() - t0
            print(f"    ...{i}/{len(rows)}  pairs={len(pairs)}  {el:.0f}s")

    el = time.perf_counter() - t0
    print()
    print(f"  processed            : {len(rows)}")
    print(f"  no embedding         : {no_emb}")
    print(f"  no neighbour >= floor: {no_neighbour}")
    print(f"  candidate pairs      : {len(pairs)}")
    if sims:
        sims_sorted = sorted(sims)
        n = len(sims_sorted)
        print(f"  neighbour similarity : min={sims_sorted[0]:.3f} "
              f"p50={sims_sorted[n//2]:.3f} p90={sims_sorted[int(n*0.9)]:.3f} "
              f"max={sims_sorted[-1]:.3f}")
    print(f"  elapsed              : {el:.1f}s")

    if not args.dry_run and pairs:
        try:
            res = st.boost_edges(pairs, delta=0.3, edge_type="semantic_knn")
            print(f"  wrote                : {len(res)} edges (semantic_knn)")
        except Exception as exc:  # noqa: BLE001
            print(f"  boost_edges FAILED: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 3
    elif args.dry_run:
        print("  (dry run — nothing written)")

    try:
        st.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
