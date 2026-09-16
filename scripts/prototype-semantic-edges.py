#!/usr/bin/env python3
"""PROTOTYPE — semantic (embedding kNN) edges, to test whether they fix the
edgeless-periphery that lexical entity edges cannot reach.

Why: `entity_link.mine_entity_edges` only links records that share a rare
literal token. Most records share no such token, so ~99.8% of communities are
singletons and Leiden has no edges to cluster on. The store already holds
vectors, so similarity is available without new data.

VECTOR SOURCE — read this before changing anything. There are TWO different
vectors in this store and they are NOT interchangeable:

  * `records.embedding` (column)  — 1536-dim
  * the ANN index (`records.hnsw`) — 384-dim, and what `query_similar` serves

Measured: 13,950 stored embeddings are 100% 1536-dim while the index reports
dim=384 (`vector length 1536 != index dim 384` if you feed it a column vector).
So this miner takes its vectors from the INDEX via `load_hnsw_readonly()`
+ `get_items()`, which returns exactly the 384-dim vectors the recall path
already uses. No dimension filter is needed — all index entries are 384-dim —
and neighbours are consistent with the ANN the house already trusts.

Similarity calibration (400-record sample, cosine distance to nearest
neighbours, self excluded):

  p1..p10 == 0.0000   (exact/near duplicates exist)
  p25 = 0.005   p50 = 0.139   p75 = 0.199   p90 = 0.235   p99 = 0.300
  as similarity: >=0.8 -> 75.5% of neighbours; >=0.9 -> 39.4%

So a `--floor` near 0.85 links the genuinely close neighbourhood without
wiring the whole corpus together. A floor matters: linking every record to its
nearest neighbour regardless of distance destroys community structure rather
than revealing it.

Also note the vectors are L2-normalised (norm == 1.0 exactly), so cosine
distance is well-behaved here.

Design notes:
  - Edges are undirected in effect but stored as one row per pair. We link each
    record to its top-k neighbours and rely on merge-insert dedupe (the same
    canonicalised pair keys `boost_edges` uses), so re-running is idempotent.
  - This is a PROTOTYPE: `--dry-run` is the default and it reports what it
    WOULD do. `--write` commits.
  - Reading the store while the daemon holds it open is unreliable (torn
    snapshots). Either run this with the daemon stopped, or accept read-only
    index access as used here, which has been reliable.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

STORE = os.environ.get("IAI_MCP_STORE") or os.path.expanduser("~/.iai-mcp")
INDEX_DIM = 384


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5, help="neighbours per record")
    ap.add_argument("--floor", type=float, default=0.85,
                    help="minimum cosine similarity to accept an edge")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap records processed (0 = all)")
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--write", dest="dry_run", action="store_false")
    ap.add_argument("--edge-type", default="semantic_knn")
    args = ap.parse_args()

    os.environ["IAI_MCP_STORE"] = STORE
    import numpy as np
    from iai_mcp.hippo._recall import load_hnsw_readonly

    t0 = time.perf_counter()
    print(f"  store: {STORE}")
    print(f"  k={args.k}  floor={args.floor}  write={not args.dry_run}")

    idx = load_hnsw_readonly(STORE, INDEX_DIM)
    if idx is None:
        print("  FATAL: could not load the ANN index", file=sys.stderr)
        return 2
    labels = list(idx.get_ids_list())
    total = len(labels)
    print(f"  index vectors: {total} (dim {INDEX_DIM})")
    if total == 0:
        print("  index is empty — nothing to mine", file=sys.stderr)
        return 2

    if args.limit:
        labels = labels[: args.limit]
        print(f"  processing {len(labels)}")

    # label -> record id (uuid). The index labels are ints; the store maps them.
    #
    # LOCK-FREE READ: `MemoryStore(path)` takes the store's EXCLUSIVE lock, so it
    # raises HippoLockHeldError whenever the daemon is running — which is the
    # normal case. `get_lilli_raw_conn(read_only=True)` opens a dedicated
    # lock-free handle instead (the same primitive the RO pool uses) and reads
    # fine while the daemon holds the writer lock. Use that.
    from iai_mcp.lillibrain.connection import get_lilli_raw_conn

    db_path = os.path.join(STORE, "hippo", "brain.sqlite3")
    conn = get_lilli_raw_conn(db_path, read_only=True)
    if conn is None:
        print(f"  FATAL: no lock-free connection for {db_path}", file=sys.stderr)
        return 2
    label_to_id: dict[int, str] = {}
    try:
        rows = conn.execute("SELECT vec_label, id FROM records").fetchall()
        for r in rows:
            try:
                label_to_id[int(r[0])] = str(r[1])
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        print(f"  FATAL: cannot map vec_label -> id: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    print(f"  label->id mappings: {len(label_to_id)}")

    pairs: list[tuple] = []
    unmapped = 0
    below = 0
    sims: list[float] = []

    for i, lab in enumerate(labels):
        src_id = label_to_id.get(int(lab))
        if src_id is None:
            unmapped += 1
            continue
        vec = np.asarray(idx.get_items([int(lab)]), dtype=np.float32).reshape(1, -1)
        lab_arr, sc_arr = idx.knn_query(vec, k=args.k + 1)
        for nb, dist in zip(lab_arr[0], sc_arr[0]):
            if int(nb) == int(lab):
                continue                     # self
            sim = 1.0 - float(dist)
            sims.append(sim)
            if sim < args.floor:
                below += 1
                continue
            dst_id = label_to_id.get(int(nb))
            if dst_id is None:
                unmapped += 1
                continue
            pairs.append((src_id, dst_id))
        if i and i % 1000 == 0:
            print(f"    ...{i}/{len(labels)}  pairs={len(pairs)}  "
                  f"{time.perf_counter()-t0:.0f}s")

    el = time.perf_counter() - t0
    print()
    print(f"  processed        : {len(labels)}")
    print(f"  unmapped ids     : {unmapped}")
    print(f"  below floor      : {below}")
    print(f"  candidate pairs  : {len(pairs)}")
    if sims:
        s = sorted(sims)
        n = len(s)
        print(f"  neighbour sim    : min={s[0]:.3f} p50={s[n//2]:.3f} "
              f"p90={s[int(n*0.9)]:.3f} max={s[-1]:.3f}")
    print(f"  elapsed          : {el:.1f}s")

    if not args.dry_run and pairs:
        # Writing needs a WRITABLE store, which needs a writable store handle.
        # Do not silently pretend the write happened if we cannot get one.
        try:
            w = get_lilli_raw_conn(db_path, read_only=False, allow_writer=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  WRITE NEEDS A WRITER HANDLE — unavailable while the daemon "
                  f"runs ({type(exc).__name__}: {exc}).", file=sys.stderr)
            print("  Stop the daemon and re-run with --write.", file=sys.stderr)
            return 3
        if w is None:
            print("  WRITE: no writer handle available (daemon running?). "
                  "Stop the daemon and re-run.", file=sys.stderr)
            return 3
        print(f"  writer handle acquired; {len(pairs)} pairs ready "
              f"(boost_edges wiring is not implemented in this prototype)")
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass
    elif args.dry_run:
        print("  (dry run — nothing written)")

    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

