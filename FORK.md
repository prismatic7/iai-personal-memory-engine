# FORK.md — what this fork changes, and why

This repository is a **public fork of iai-pme**, maintained by
[prismatic7](https://github.com/prismatic7) for memory-footprint and retrieval-latency
work that upstream has not taken. It exists so those changes are inspectable, and so
upstream history is shared rather than reconstructed.

## Upstream

| | |
|---|---|
| Repository | **<https://github.com/CodeAbra/iai-personal-memory-engine>** |
| Package | **`iai-pme`** (its Python module is `iai_mcp`) |
| Version forked | **3.2.3** (tag `v3.2.3`, commit `ece3cff`) |
| Author | **Areg Noya** (`CodeAbra`) — © 2026 |
| Licence | **MIT** (see [`LICENSE`](./LICENSE)) |

This is a **true GitHub fork**: `main` is a clean mirror of upstream, and every local
change lives on the **`customizations`** branch. Keeping `main` pristine means syncing
with upstream is an ordinary merge, not a replay:

```bash
git fetch upstream
git checkout customizations
git merge upstream/main          # resolve only where we touch the same lines
```

Nothing here is a reimplementation of upstream files beyond the specific changes listed
below. If a hunk is unclear, `git diff upstream/main...customizations` is the full story.

## Changes on `customizations`

Eight commits, in dependency order:

### 1. RO-pool page caches are unbounded → bounded

`HippoDB.__init__` bounds its **writer** connection with `PRAGMA cache_size=-65536`
(64 MiB). The read-only pool does not go through `HippoDB` — it calls
`get_lilli_raw_conn()` directly — so all `RO_POOL_SIZE` (8) slots were opened with the
pager's `_max_cached_pages` unset, i.e. **unbounded**.

The store is a pure-Python B+tree engine (LilliBrain), not SQLite, and `Pager` retains
whole pages as `bytearray`. That retention is engine-side, so `gc.collect()` cannot touch
it — measured: **0.0 MiB reclaimed across 108 relief records**.

Measured on a 12k-record / 48 MB store (full-store scan, all tables):

| Per-slot bound | Rows read | Peak retained |
|---|---|---|
| unbounded (upstream) | 66,313 | **+368.2 MB** |
| 16 MiB (this fork) | 66,313 | **+6.0 MB** |

Identical row counts at every bound — this is a pure footprint change.

Live daemon, before vs after: a **+250–351 MB per sleep cycle** staircase (peak 848 MB →
2128 MB over 21 days) became **flat, +0.5 MB over 3 minutes**.

**Knob:** `IAI_MCP_RO_POOL_CACHE_KIB` (default `16384` = 16 MiB per slot, so the pool's
ceiling is 128 MiB). `0` restores upstream's unbounded behaviour, which is how the
comparison above was made.

### 2. Read models were never republished after a write

Every indexed point read on a refreshed RO-pool slot paid a whole-table lazy index build.
The engine's id index is per-connection and in-memory only (no on-disk id index exists;
only `records.colindex` is persisted), so a new connection must build it from a full scan.
The pool closes and reopens a slot whenever the writer commits
(`mark_ro_pool_stale` → generation bump → refresh on next borrow), but
`publish_read_models()` — the mechanism that builds the col/id index pairs once so readers
*adopt* them — was called at boot only. The generation bumped, nothing was republished, so
every post-write refresh rescanned.

Measured on a 13.6k-record / 48 MB store, first indexed point lookup:

| State | Cells scanned | Time |
|---|---|---|
| cold slot, nothing published | 13,654 | 1,016 ms |
| after `mark_stale` only (upstream) | 13,654 | 1,005 ms |
| after republish (this fork) | 0 | 0.23 ms |

In production this was ~20 minutes of stall in one logged window. `publish_read_models` is
idempotent (~70 ms) and the engine's adoption re-verifies a per-table write-generation
stamp, so a reader can never adopt stale rows — a mismatched stamp is simply not adopted
and the lazy build remains the correct fallback.

### 3. Skill-load preamble leaked into the rendered continuity block

Some agent harnesses set a session's opening turn to a skill-load notice:

```
[IMPORTANT: The user has invoked the "x" skill, indicating they want you to
follow its instructions. The full skill content is loaded below.]
```

That turn seeds the working-tier *goal* verbatim, so the notice reached the injected
continuity surface wearing directive framing it has no standing to carry. Stripped in
`_clean_surface` — at the **render** boundary only, so stored records stay verbatim and the
archive keeps what actually happened.

### 4. In-block session owner, alongside upstream's sidecar

Upstream 3.2.3 already prevents a session receiving another session's goal, via a sidecar
(`.session-continuity.state.json`) recording the last writing session; the recall hook's
`session_scope_blocks()` refuses to emit when it differs.

This fork keeps that sidecar as **primary** and adds an *in-block* `session:` owner as
defence in depth, because a sidecar is a separate file and so can be absent (older daemon),
deleted, or never written by a read-only session — in which case upstream's check fails
**open** and one session's goal can still surface in another. The hook honours both gates
and still fails open when neither source knows, so upgrading users do not lose continuity.

### 5. Entity-link minting caps are env-overridable

The edge-minting caps were fixed constants; they now read from the environment so tuning
does not require a patch.

### 6. Semantic (embedding kNN) edges in the sleep pipeline

A new `semantic_edges.py` plus a `_semantic_link` step wired into the sleep pipeline, with
the supporting `store/_exact_index.py` surface. `scripts/prototype-semantic-edges.py`
records the working prototype the step was derived from (lock-free reads,
index-sourced vectors).

### 7. Lexical rebuild and tier-0 induction are streamed

The lexical index rebuild and tier-0 schema induction previously materialised their work;
both stream, which cuts peak memory on large stores.

### 8. Recovery / tooling scripts

- `scripts/recover-stranded-key.py` — multi-column key-wipe recovery (the original pass
  missed `provenance_json`).
- `scripts/prototype-semantic-edges.py` — the semantic-edge prototype (see #6).

## Relationship to the running install

The live daemon on the maintainer's machine runs this branch **installed as a wheel**
into `~/Development/iai-pme-venv` (non-editable — the checkout has no generated
`src/iai_mcp/_wrapper/`, so an editable install leaves the MCP path missing). The old
`patches/reapply.sh` in-place-patching era is over; `FORK.md` is now the record and the
wheel is the artefact.

### Deploying a change to the live daemon

**Anything under `crates/` requires a native rebuild.** Python-only changes still need
the wheel path, because the daemon, both Hermes MCP configs and the CLI all load
`iai_mcp` from the venv's `site-packages` by absolute path — a source checkout alone
changes nothing.

```bash
cd ~/Development/iai-personal-memory-engine

# 1. Back up the store AND the installed .so (the rollback artefact).
BK=~/Development/iai-deploy-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$BK"
cp ~/.iai-mcp/hippo/brain.sqlite3{,-wal} ~/.iai-mcp/hippo/records.{colindex,hnsw} "$BK"/
cp ~/.iai-mcp/.crypto.key "$BK"/
cp ~/Development/iai-pme-venv/lib/python3.11/site-packages/iai_mcp_native.cpython-311-darwin.so \
   "$BK"/iai_mcp_native.PRE_DEPLOY.so

# 2. Build the wheel (recompiles the extension from crates/ — ~2 min warm).
NODE_ENV=development IAI_MCP_WRAPPER_PREBUILT=1 uv build --wheel \
  --python ~/Development/iai-pme-venv/bin/python --out-dir /tmp/iai-deploy-wheels

# 3. VERIFY the new .so is really in the wheel before installing.
mkdir -p /tmp/wc && (cd /tmp/wc && unzip -oq /tmp/iai-deploy-wheels/*.whl)
shasum -a 256 /tmp/wc/iai_mcp_native*.so   # must differ from the installed one

# 4. Stop, install, start.
~/Development/iai-pme-venv/bin/iai-mcp daemon stop
uv pip install --python ~/Development/iai-pme-venv/bin/python --reinstall \
  /tmp/iai-deploy-wheels/iai_pme-3.2.3-cp311-cp311-macosx_11_0_arm64.whl
~/Development/iai-pme-venv/bin/iai-mcp daemon start

# 5. Poll until it actually answers (boot warmup takes ~20-60 s; an immediate
#    `daemon status` prints "daemon not running" and is NOT a failed start).
for i in $(seq 1 24); do
  ~/Development/iai-pme-venv/bin/iai-mcp daemon status 2>&1 | grep -q "state:" && break
  sleep 10
done
```

**⚠️ The wheel cache the nightly guard restores from.** `iai-fork-install-guard.sh`
repairs a clobbered install from `~/Development/iai-pme-venv/wheels/`. If that cached
wheel is older than the change you just deployed, the guard silently **rolls the native
engine back**. After every deploy that touches `crates/`, refresh the cache and confirm
the `.so` inside it:

```bash
cp /tmp/iai-deploy-wheels/*.whl ~/Development/iai-pme-venv/wheels/
# then unzip it and hash the .so again — a same-named wheel is not sufficient proof
```

A wheel's *file* hash changes on every build (zip timestamps), so file hashes cannot
tell you which engine is inside. **Always unzip and hash the `.so`.**

### Verifying a deploy took

Neither the daemon's start time nor the install timestamp proves which `.so` is live.
Check the mapping directly:

```bash
PID=$(pgrep -f iai_mcp.daemon)
lsof -p "$PID" | grep iai_mcp_native            # inode + size the daemon holds
stat -f "inode=%i size=%z" <the installed .so>  # must match
```

Then confirm data integrity and the write path, both **lock-free** (the daemon holds an
exclusive lock, so `HippoDB(...)` raises `HippoLockHeldError` — use
`get_lilli_raw_conn(path, read_only=True)`):

- Record and edge counts unchanged from the pre-deploy baseline.
- The `tests/` lanes that exercise the engine
  (`test_native_guard`, `test_lillibrain_btree_insert_split`,
  `test_lillibrain_btree_delete_merge`, `test_lillibrain_btree_max_key`,
  `test_lillibrain_rust_parity`, `test_lillibrain_rust_linearity`,
  `test_lillibrain_connection_concurrency`, `test_lillibrain_corruption_surfaces`)
  green with `IAI_MCP_STORE` pointed at a throwaway dir.
- `iai-fork-install-guard.sh` silent (exit 0, empty stdout = healthy).

`tests/test_engine_differential_fuzz.py` needs `hypothesis`, which is not installed in
the runtime venv; it fails at *collection*, not on a real assertion. Drop it from the
lane rather than treating the collection error as a regression.

### Pre-flight without touching the live install

To validate a new `.so` against real data before deploying: copy the store, then load
the candidate `.so` from its own directory with that directory first on `sys.path`.
Both engines read the copy independently and their counts must agree exactly —
16,038 records / 47,273 edges on the 2026-09-17 store. This catches a broken engine
before it is anywhere near the live store.

## Notes carried over from the sdist-based era

This fork was originally assembled from the **PyPI sdist** (`iai_pme-3.2.3.tar.gz`) before
a public git repository was found. The sdist is upstream's source but not its whole tree —
it omits ~420 files the repo carries (`crates/`, `bench/`, `tests/`, `fixtures/`, `.github/`,
`docs/`, `desktop/`, `plugin/`), including `crates/` — the storage engine `iai_mcp_native`
links against.

Two consequences worth recording, since both are now resolved by forking git directly:

1. **The native engine is rebuildable.** All three crates (`lilli-hd`, `lillibrain`,
   `lilliengine`) are present here, as is the root `Cargo.toml` workspace manifest. An
   earlier conclusion that they were unpublished and the extension unrebuildable was
   **wrong** — it was drawn from the sdist alone.
2. **The sdist-only scaffolding commits are gone.** The old branch carried a reconstructed
   `rust/Cargo.toml` and an `mcp-wrapper/node_modules` untracking commit; both existed to
   work around sdist omissions and are unnecessary now that upstream's real tree is the
   base.

## Building and testing

```bash
# Python layer loads straight from src/ (no build step)
PYTHONPATH=./src python -m iai_mcp.cli --help

# the suites covering the changes above
python -m pytest tests/test_sleep_overhaul.py \
                 tests/test_recall_index_overlay.py \
                 tests/test_cli_maintenance_sleep_cycle.py \
                 tests/test_recall_ro_pool.py \
                 tests/test_ro_pool_refresh.py
```

The Rust extension builds from this tree (workspace manifest and `crates/` are present):

```bash
cargo build --release -p iai_mcp_native
```
