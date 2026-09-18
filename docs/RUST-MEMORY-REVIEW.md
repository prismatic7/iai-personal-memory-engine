# Rust crate memory-footprint review

Scope: the four in-tree crates (`lillibrain`, `lilliengine`, `lilli-hd`, `lilli-parity`)
that back the native extension. Written after the fork moved from the PyPI sdist to
a direct git fork, which is what made `crates/` available at all — the sdist omits it.

Everything below is **measured**, on an M2 Max, `cargo --release`, with a counting
`GlobalAlloc` so the numbers are *bytes allocated* rather than wall time. Timing
hides allocation churn; a warm-cache read that allocates two page buffers is pure
waste whether or not it is fast.

Reproduce:

```bash
cargo run --release -p lillibrain --example alloc_probe
cargo run --release -p lillibrain --example cache_bound_probe
cargo run --release -p lillibrain --example write_path_probe
cargo run --release -p lilli-hd   --example hv_probe
cargo run --release -p lilli-hd   --example bundle_alloc_probe
```

---

## Context: the live daemon

| | |
|---|---|
| Daemon RSS (12.5 h uptime) | **1.84 GB** |
| Physical footprint / peak | **1.1 GB / 1.9 GB** |
| Store on disk (`~/.iai-mcp/hippo/`) | **95 MB** (`brain.sqlite3` 63.5 MB) |
| Engine page cache, if unbounded | grows to the size of the file |

A 95 MB store against a 1.1 GB footprint means the resident working set is not the
data. The findings below are where the multiple goes.

---

## Finding 1 — the page-cache bound is unenforceable during a write transaction

**Severity: high.** This is the largest single lever and it defeats the bound exactly
when the bound matters.

`Pager::evict_if_over_budget_protecting` never evicts a **dirty** page (correctness
over budget). During an open write transaction every page the transaction touched is
dirty, and the dirty set is only cleared at `commit`. So while a write burst runs, the
eviction pass finds no candidate and the cache grows without limit.

The production write path is exactly one big transaction: row keys are allocated
`max_key() + 1` (strictly ascending appends), and the host batches the whole
insert run inside a single `begin_write` / `commit`.

Measured with a **256-page (2 MiB)** bound:

```
INSIDE the open transaction (every touched page is dirty):
  after      1 rows:      4 pages resident  (     1x the bound)
  after   2001 rows:    503 pages resident  (     2x the bound)
  after   4001 rows:   1003 pages resident  (     4x the bound)
  after   6001 rows:   1505 pages resident  (     6x the bound)
  after   8000 rows:   2004 pages resident  (     8x the bound)

  resident 2004 pages = 15 MiB held in the page cache,
  despite a bound of 256 pages. Eviction found no clean candidate.
```

The write pattern is the aggravating factor. `write_path_probe` measures a sequential
append — which is the production shape — against the same bound:

```
=== A. sequential append (ascending keys, rightmost-leaf growth) ===
  bound 64 pages. Resident pages as rows accumulate:
      2000 rows ->    502 resident
      4000 rows ->   1002 resident
      6000 rows ->   1504 resident
      8000 rows ->   2004 resident
  db_size 2006 pages; residency 100% of the file
  The live working set for this access pattern is O(1) pages:
  every insert descends the same interior path to the same leaf.
```

**Residency is 100% of the file while the live working set is O(1) pages.** Every
insert descends the same interior path and lands on the same rightmost leaf; the
~2000 other resident pages are held, dirty, and never revisited before commit.

For contrast, a random-order update of a quarter of the rows also reaches 100% — but
there residency genuinely *is* the working set, so it is not avoidable. The sequential
append is the avoidable case and it is the production case.

**Corroborated from a second, independent probe.** `alloc_probe` probe [5] was written
to measure eviction-pass *allocation overhead* (bounded cache vs unbounded) and is not
aimed at Finding 1 at all — but it runs the same 3000-row insert burst inside one
transaction, and its resident-page readout lands on the same defect:

```
[5] insert burst 3000 rows (~606 pages):
    bounded cache (256 pages, resident 752)    230065053 B
    unbounded cache (resident 754)              230064925 B
    eviction pass overhead                             128 B  (0 B/row)
```

Three things worth reading off this. (1) **Resident 752 under a 256-page bound** — the
same unbounded growth, from a different code path, in a probe that never set out to find
it. (2) The bound is a *no-op* here: 752 vs 754 resident is a 2-page difference, and the
eviction pass's own cost is 128 B total. A bound that does not change residency is not
bounding anything. (3) The burst is only ~606 pages of data, so residency exceeds the
data itself — this is not "the cache holds the working set", it is the cache holding
every page the transaction touched, exactly as this finding predicts.

**Method note — a redaction artifact, recorded because it looked like a bug.** In that
same probe run, `[4]`'s peak printed as `peak +4****20 B`. That is *not* a sentinel the
probe emits: the on-disk file contains `4154820`, and the `****` is introduced by the
display layer when it treats a digit run as secret-shaped. Recovered by reading the raw
bytes (`re.findall(rb"\d+", line)` → `4154820`). Any probe number in this domain that
comes back oddly shaped should be re-read from the file before being interpreted.

**Why the existing bound did not catch this.** `IAI_MCP_RO_POOL_CACHE_KIB` bounds the
read-only pool's *readers*. The writer connection goes through `HippoDB` with
`PRAGMA cache_size=-65536`, and the host also runs an RSS soft-cap
(`IAI_MCP_REEMBED_RSS_SOFT_CAP_BYTES`, default 2.5 GiB) — but that cap gates batch
*scheduling*, not pager residency, and 2.5 GiB sits above the footprint being observed.
Neither mechanism bounds the writer's dirty set mid-transaction.

### Fix directions

1. **Spill a dirty page to the WAL and drop it.** In WAL mode the log is already the
   durable record the commit will use, so a dirty page that is not part of the live
   descent path can be appended as a frame and evicted, while the transaction stays
   open and rollback stays correct. This is the principled fix and it is
   correctness-sensitive: `rollback()` currently reloads dirty pages from the
   WAL index, so the index must be consulted rather than assumed.
2. **Cap the dirty set by bytes, not pages.** A separate `max_dirty_bytes` with the
   same "correctness over budget" escape hatch, so the failure is bounded and
   observable rather than open-ended.
3. **Chunk the host-side write transaction.** The cheapest mitigation, and it needs
   no engine change: the sleep pipeline's insert burst becomes N commits instead of
   one. It trades some WAL amortisation for a hard bound and is worth doing on its
   own merits, independently of the engine fix.

---

## Finding 2 — every warm-cache page read clones the whole page (and the header twice over)

**Severity: medium-high.** Fixed in this pass for the header half; the same shape
remains elsewhere.

`read_page` is the pager's hottest entry point. It cloned the 8 KiB header page out of
the cache *and* the requested page, so a warm-cache read allocated **16,384 B** for an
8,192 B page — 2x, on every read.

`ensure_present` already exists and reads or updates a page **in place**; the header
only ever needed four bytes (`db_size` for the bounds check).

Measured before → after:

| Operation (warm cache) | Before | After |
|---|---|---|
| `pager.read_page` | 16,384 B | **8,192 B** (−50%) |
| point `get` | 79,608 B | **46,840 B** (−41%) |
| `get_many` (64 keys) | 5,046,528 B | **3,645,696 B** (−28%) |
| insert burst, 3000 rows | 384,900,061 B | **268,245,981 B** (−30%) |

Two `ensure_cached`-for-a-header-field sites were fixed the same way:

- `read_page` → now uses a new `db_size_cached` helper that reads in place.
- `read_header_u32` (backing `db_size`, `freelist_count`, `first_freelist_trunk`)
  → `extend_file` calls `db_size` once per page allocation, so a bulk append was
  paying one discarded 8 KiB clone per page written. This is most of the −30% above.

`commit()` also calls `db_size()` twice (`collect_dirty`, then the barrier), and
`begin_write`/`rollback` once each — same helper, now cheap.

The remaining per-read clone is the requested page itself (8,192 B for 8,192 B). That
one is a real API decision rather than a bug: ~70 call sites across `cursor`,
`overflow`, `split`, `delete`, `freelist`, `check`, `wal`, and `meta` take
`Vec<u8>` and treat the page as owned scratch (rebuilt page → `write_page`,
`set_sibling` mutating in place). Returning `Arc<[u8]>` would remove the clone, but
every mutating site needs copy-on-write, so it is a coordinated change across those
files rather than a local one. Worth scoping; not a one-line fix.

---

## Finding 3 — `bundle_impl` allocates the SWAR ladder once per 64-bit word

**Severity: medium. APPLIED — see the fix at the end of this section.**

`bundle_impl` accumulates per-bit-position counts in a bit-sliced SWAR ladder
(`planes: Vec<u64>`, one plane per count bit). The ladder is re-allocated **inside**
the per-word loop, even though its width is fully known before the loop starts.

On a 10,000-bit hypervector with 100 inputs:

```
  output = 1250 B; inner loop = 156 words x 7-plane ladder

  total allocated      9986 B
  allocation calls     157
  unavoidable (output) 1250 B in 1 call
  ladder churn         ~8736 B in ~155 calls  (7.0x the output)
```

156 of the 157 allocations are the ladder. Hoisting it above the word loop (and
zeroing it per word, or reusing a scratch buffer) leaves **1,250 B + 56 B per call**.

Bundle is the majority-vote step of every BSC `bundle` / `unbind` on the episodic
tier, so on a binding-heavy path this is continuous allocator churn of ~8 KB per call,
all of it transient.

### APPLIED — fixed stack ladder, measured

The ladder is per-word *scratch*, not per-call state: `bit_width` derives from `n` and
is therefore fixed for the whole call, and every word restarts counting from zero. So
the allocation was pure waste. Since `n: u32`, `bit_width <= 32`, a fixed
`[u64; 32]` stack array covers every possible width and allocates nothing.

```rust
let mut planes = [0u64; u32::BITS as usize];   // hoisted, once per call

for w in 0..n_words {
    planes[..bit_width].fill(0);               // re-zeroed per word
    for hv in bound_hvs {
        let mut carry = u64::from_be_bytes(hv[off..off + 8].try_into().unwrap());
        for plane in planes[..bit_width].iter_mut() { /* ripple-add */ }
    }
    let result = majority_from_planes(&planes[..bit_width], n);
}
```

Measured with `bundle_alloc_probe` (D = 10000, 100 inputs):

| | before | after |
|---|---|---|
| total allocated | 9,986 B | **1,250 B** |
| allocation calls | 157 | **1** |
| ladder churn | ~8,736 B in ~155 calls | **0 B in 0 calls** |

That is exactly the predicted residual (output only) — the hoist landed on the
theoretical optimum rather than merely improving it.

**Correctness evidence.** The two bundle parity gates pass, and they are
cross-language: `fixtures/golden/hdc/bsc_ops.bin` and `bsc_bundle_10000.bin` are frozen
from the Python reference by `scripts/golden/dump_hdc_ops.py`, which imports
`iai_mcp.lilli.tiers.bsc`. So the Rust kernel is checked byte-for-byte against the
Python implementation, not against itself.

- `bsc_ops_byte_identical` — n = 3, 7, 10 at D = 4096 (word-aligned, tail empty).
- `bsc_bundle_d10000_tail_byte_identical` — n = 3, 7, 10 at D = 10000 ⇒ 156 words + a
  2-byte tail, the only case that drives the scalar tail vote.

The n values matter twice over: `bit_width = floor(log2 n) + 1` differs across them, so
the three cases exercise **different ladder widths** (2, 3, and 4 planes) — which is
precisely the dimension this change touches. `lilli-hd` is 25/25 green after the change.

---

## Finding 4 — `permute_impl` expands to one byte per bit

**Severity: medium. APPLIED — see the fix at the end of this section.**

`permute_impl` unpacks the packed hypervector to a bit vector (1 byte/bit), rolls it,
then repacks:

```
[permute]         21250 B allocated for a 1250 B hypervector  (17.0x the payload)
[bind]             1250 B allocated  (1.0x)
[hamming]             0 B allocated  (popcount path, zero-copy)
```

The circular bit-roll is directly expressible in the packed domain (word rotate plus a
residual bit shift across the word boundary), so the unpack is not required. It serves
two purposes — parity with `np.roll` semantics and a simple indexing argument — and
the crate's own note claims parity is the constraint, not performance.

For calibration, `bind` (1x) and `hamming` (0 B, hardware popcount) show the kernels
are otherwise tight. `permute` is the outlier.

### APPLIED — packed-domain rotate, measured

This one is production-routed, unlike the projection kernel: `src/iai_mcp/lilli/tiers/bsc.py:194`
dispatches `permute` to `backend.native().bsc_permute` under `IAI_MCP_HD_BACKEND=rust`.

The bit length is always `8 * len`, so a rotation decomposes as `s = 8a + r` — a
whole-byte rotation plus a residual in-byte shift — and each output byte draws from
exactly two input bytes: the one `a` back, and the one before it for the bits crossing
the byte boundary. The unpack is therefore unnecessary.

```rust
let a = (s / 8) as usize;
let r = (s % 8) as u32;
for (b, slot) in out.iter_mut().enumerate() {
    let hi = (b + len - a) % len;
    let lo = (hi + len - 1) % len;
    *slot = if r == 0 { hv[hi] } else { (hv[hi] >> r) | (hv[lo] << (8 - r)) };
}
```

**One trap, worth recording.** Bit index and mask are *reversed* under MSB-first
packing: bit `j` has mask `1 << (7 - j)`. So a bit moving *up* by `r` indices moves
*down* by `r` in mask position — a **right** shift. The natural first draft (`<< r`)
is wrong, and the algebra alone does not flag it; a hand-check against the reference
did (`len=2, hv[0]=0x80, shift=1` must give `0x40`, the `<<` form gives `0x00`).

Measured with `hv_probe` (D = 10000, 1250 B payload):

| | before | after |
|---|---|---|
| allocated | 21,250 B (17.0x payload) | **1,250 B (1.0x)** |

Now level with `bind` (1.0x); the three kernels are consistent.

**Correctness evidence — and a negative control.** The existing coverage was thinner
than it looked, so a differential gate was added before trusting the change:

- `laws::bsc_permute_round_trip` (pre-existing) is a round-trip *law*. It holds for any
  self-consistent direction or asymmetry convention, so it **cannot** catch a wrong
  rotate direction or a swapped residual-bit order.
- `bsc_ops_byte_identical` (pre-existing) is cross-language — frozen from Python
  `np.roll` by `scripts/golden/dump_hdc_ops.py` — so it does pin the convention, but on
  only 4 shifts (1, 7, 33, 4095) over a single vector.
- `laws::bsc_permute_matches_scalar_reference` (**added**) asserts the packed kernel
  against the *original* unpack/roll/repack body, inlined as the oracle — i.e. against
  exactly the implementation being replaced — over `len` 1..64 and `shift` −1000..1000,
  which sweeps `r = 0`, `|shift| > n`, negatives, and wrap at both ends.

The gate was then **falsified on purpose**: injecting the mask-direction bug above
(`>> r` → `<< r`) makes `bsc_permute_matches_scalar_reference` and
`bsc_ops_byte_identical` fail while `bsc_permute_round_trip` still **passes** —
confirming both that the new gate has teeth and that the pre-existing round-trip law is
genuinely blind to this class of error. Source restored afterwards (sha256
`ce24584e5b423bb5659cfdebec26789d056caa7924e5cde5d418c4397debd210`); `lilli-hd` 26/26 green.

Note: `unpack_bits_msb` / `pack_bits_msb` are `pub` but now have **no callers anywhere**
in the workspace — the rewrite orphaned them. Left in place deliberately (public API,
and they document the packed layout); flagged here so the dead-code state is not a
surprise.

---

## Finding 5 — `read_leaf_keys` materialises the full key array per node visit

**Severity: low-medium. APPLIED — see the fix at the end of this section.**

`move_to` descends to a leaf and then calls `read_leaf_keys(&page, ...)` to build a
`Vec<i64>` of every key on the page, purely to `bisect_left` one index. Same in
`insert`, and `read_interior_node` allocates `keys` **and** `children` per interior
node visited.

On a wide table (~4 cells/page) this is only 4 entries — an allocation, not a
blowup. But it is the reason a point `get` still allocates 46 KB against a 1,656 B
payload, alongside the descent's page clones. A binary search that reads cell keys
directly from the page, without building the array, removes it.

### APPLIED — in-place page search, and the descent's duplicate read

The premise that made this cheap to fix: **the leaf pointer array is an O(1) index**,
so the "sorted array" a binary search needs is an index *into the page*, not a heap
copy of it. Cell `i`'s key is a `read_leaf_cell_raw(page, i, threshold)` away, which
decodes framing in place and allocates nothing.

Three changes, in order of measured effect:

1. **The descent returned a page number the caller immediately re-read.**
   `move_to` read and cloned the leaf to identify it; `get` then did
   `read_page(leaf_page_no)` and cloned it *again* — two 8 KiB clones for one logical
   leaf access. `move_to` now returns the buffer it already holds (a move, not a
   copy), and `get`/`insert` consume it. This is one page read removed per lookup.
2. **`leaf_bisect_left`** replaces `bisect_left(&read_leaf_keys(...)?, k)` on the
   descent and in `insert`. In `insert` the array also supplied `keys.len()` (which
   is now `read_leaf_header(&page).num_cells`) and one `keys[insert_idx]` (now a
   single in-place cell decode). Ascending appends land on the *rightmost* leaf of
   the table, so that was the widest array on the hot write path.
3. **`interior_child_index` / `interior_child_ptr`** replace
   `bisect_right(&read_interior_node(&page)?.keys, k)` on the interior descent, which
   allocated a key `Vec` **and** a child `Vec` per level of every descent.

Measured with `alloc_probe` (N = 6000, 1,656 B records):

| | before | after |
|---|---|---|
| warm point `get` | 46,840 B | **26,296 B (-44%)** |
| `get_many` (64 keys) | 56,964 B/key | **43,289 B/key (-24%)** |
| page reads per point lookup | 4 | **3** |

Both original call sites remain exported and used elsewhere (`bisect_left` by
`delete.rs`/`split.rs`, `read_interior_node` by `delete.rs`/`split.rs`/`check.rs`), so
nothing was orphaned — `check.rs` also remains the path that validates *every* cell on
a page, whereas the in-place search validates every cell it probes.

**Correctness evidence — and a negative control.** `btree_ops.rs` gained four tests
that use the *replaced* expressions as the oracle:

- `leaf_in_place_bisect_matches_the_materialised_key_array` — leaf widths 0/1/2/3/17/64/200,
  duplicate keys, probes swept past both ends of the key range.
- `interior_in_place_child_selection_matches_the_materialised_node` — widths 1..128,
  asserting **both** the selected index and the resolved child pointer, which covers
  the rightmost child that lives in the header rather than the cell array.
- `interior_child_ptr_rejects_an_index_past_the_rightmost_child` — the out-of-range
  index the slice it replaced would have panicked on.
- `point_get_agrees_with_a_full_scan_across_many_shapes` — end-to-end, present /
  absent / boundary / overflow-spilled keys, cross-checked against `range_scan` so a
  descent bug cannot hide behind a matching sibling-chain walk.

Falsified on purpose: swapping *both* bisects to the opposite variant (`<` → `<=` on
the leaf, `<=` → `<` on the interior) makes all three differential tests fail
immediately (`leaf n=1 key=0: in-place 1 != materialised 0`). The pre-existing
`cursor_insert_replaces_in_place` failed too, independently corroborating the
injection rather than relying only on the new gates. Source restored afterwards
(`page.rs` sha256 `828aca595bd1f9c8f8028bd7f84de6003842ee48ab0b285cb9c6ca84ebe9389d`).

**A rejected change, recorded so it is not re-attempted.** `delete` also re-reads its
leaf after the descent, and reusing the descent's buffer looks like the same win. It is
**not**: `remove_cell_from_leaf` rewrites the page before the underflow check reads
`num_cells`, so the descent's buffer is the PRE-removal image and the count would be
stale. `delete` still re-reads deliberately, with a comment saying why.

**Also considered, and deliberately NOT done: the `get_many` fallback descent.** When
`get_many`'s ordered walk exhausts its page budget it finishes the outstanding keys with
per-key `self.get(key)` calls, each re-descending from the root. Routing each remaining
key through `seek` instead would reuse the leaf already read — but `get_many` calls
`read_page_scan`, which explicitly does NOT insert into the cache, so under an exhausted
budget every `seek` would be a genuine cache miss and would read anyway. The saving is
real but small, and it interacts with the deliberate non-caching the scan path relies on
to avoid thrashing the LRU. Left alone on purpose rather than half-optimised.

---

## Finding 6 — the 14.65 MiB projection matrix is materialised twice

**Severity: low (latent).** Not currently costing anything — worth knowing.

`projection.rs` embeds the frozen matrix with `include_bytes!` (14,653,440 B in the
binary) and then `decode_matrix` collects it into a `Vec<f32>` — a second 14.65 MiB on
the heap, ~29.3 MiB total for a matrix that is read-only.

**This is latent, not live.** Verified: `hd.project` is called only from
`tests/lilli/test_hd_differential.py`. Production projection is numpy
(`lilli/core/projection.py` does `emb @ P` against its own RNG-generated `P`), and
`hd_backend.py` documents the choice deliberately — the matmul is memory-bandwidth-
bound at the BLAS ceiling and the native kernel is only the parity reference. So
`load_p()` is never initialised in the daemon.

Two things follow:

1. No urgent action. If it is ever wired into production, the heap copy should become
   a `Vec<f32>` built once at build time, or the f32s should stay as bytes and be
   read via `from_le_bytes` — the matrix is only ever indexed, never mutated.
2. The daemon's ~2.3 MiB of Python-side `P` (384 × 10000 × 4 B, generated by
   `default_rng`) is the live copy, and it is regenerated on every process start with
   an RNG plus a sha256 self-check. That is a startup cost, not a footprint one.

---

## What is already well-tuned

Worth recording so a later pass does not re-litigate it:

- **Scan-resistant reads.** `read_page_scan` and `count_leaf_cells` load a
  non-resident page transiently and do **not** insert it, so a full scan makes zero
  evictions and is O(corpus) rather than O(corpus × cache). The header-in-place and
  no-clone patterns are deliberate and documented.
- **`hamming_bits`** is a hardware popcount over `u64` chunks: **0 B allocated**.
- **`bind_impl`** allocates exactly its output (1x).
- **`IdIndex` / `ColIndex`** sit behind `Arc` with copy-on-write, so a `Clone` is a
  refcount bump and a decoded sidecar snapshot is shared across every read-only
  connection. Only a mutating holder pays `Arc::make_mut`.
- **`IdIndex::ensure_built`** decodes only the `id` column, never the wide embedding
  payload, so a lazy rebuild is a streaming pass rather than a whole-table decode.
- **`Store::counts`** recovers from a poisoned mutex by clearing and repopulating,
  rather than serving a torn count.

---

## Suggested order of work

| # | Change | Lever | Risk |
|---|---|---|---|
| 1 | Chunk the host-side write transaction | bounds Finding 1 today, no engine change | low |
| 2 | Cap the dirty set by bytes | bounds Finding 1 at the engine | low |
| 3 | Spill dirty pages to the WAL and evict | removes Finding 1 | high — touches rollback |
| 4 | Hoist the bundle ladder | Finding 3, 157 → 1 alloc | **APPLIED** — 9,986 B → 1,250 B, parity green |
| 5 | Packed-domain `permute` | Finding 4, 17x → ~1x | **APPLIED** — 21,250 B → 1,250 B, cross-language parity + new differential gate |
| 6 | Scoped point-get page clones / key arrays | Finding 2 residual, Finding 5 | **APPLIED** — 46,840 → 26,296 B, 4 → 3 page reads/lookup, differential gates |

Items 4 and 5 are self-contained and carry parity tests
(`crates/lilli-hd/tests/golden_parity.rs`, `tests/lilli/test_hd_differential.py`),
so they are the safe first moves if the goal is to bank a win before the harder
engine work.

---

## Status of the changes in this pass

**Applied** in `crates/lillibrain/src/pager.rs` (Finding 2's header-clone fixes):
a new `db_size_cached` helper, `read_page` switched to it, and `read_header_u32`
switched to in-place reads. Findings 1, 3, 4, 5 and 6 are **diagnosed, not yet
implemented**.

**Applied** in `crates/lillibrain/src/pager.rs` and
`crates/lillibrain/tests/crash_recovery.rs`: a real read-only snapshot-fence bug,
`ro_reader_under_concurrent_checkpoint_is_consistent_or_typed_error`.

Two distinct failure shapes had to be separated, and collapsing them was my own
first mistake:

1. `SnapshotFence { .. }` — the fence firing before the per-page checksum. The
   test's match arm accepted only `Integrity` and `CrcMismatch`, so it panicked on
   a *correct* engine outcome. Test gap.
2. `PageOutOfBounds { page_no: 32, db_size: 31 }` — the **actual engine bug**, at
   roughly 1 in 350 runs. `Pager::read_page` bounds-checks against the **cached**
   header, and a cache hit never re-validates the snapshot fence. A read-only
   reader can hold generation N's header (`db_size = 31`) while an interior node it
   cached from generation N+1 legitimately points at page 32, because a concurrent
   checkpoint grew the file underneath it. The reader then reports a *corruption*
   error for what is really "a generation behind" — worse than a cosmetic problem,
   because the fence-retry loop keys on the retryable error class and so never
   catches this one.

Fix: on the bounds-failure path only, consult the fence before reporting, so the
accurate retryable class surfaces. Re-validating only there keeps the check off
the hot path — a successful read pays nothing, and a genuine out-of-range
reference still reports `PageOutOfBounds`. The call is a strict no-op for
read-write pagers: `ro_snapshot_fence` is `Some` only in `open_read_only`
(`pager.rs:348`, inside the fn at `:317`); `open` sets it `None` (`:227`).

Attribution was measured, not argued — 560 runs each under 8-way contention:

| Build | Failures / 560 |
|---|---|
| pristine (`pager.rs` reverted) | **11** (~2.0%) |
| with the header-clone fix only | 3 (~0.5%) |
| with the fence fix | **0** |

All 14 captured failures across the pristine and intermediate builds were the
`PageOutOfBounds` shape; **zero** were the `Ok`-arm torn-read assertion, so the
safety invariant itself never broke — the bug was error *classification*, not
silent corruption. That distinction is the whole point of the test, and it held.

### Why the misclassification mattered (not cosmetic)

`PageOutOfBounds` and `SnapshotFence` land in **different Python exception
hierarchies**, and the read-only pool's retry depends on that split:

- `crates/lillibrain/src/py.rs` maps `PageOutOfBounds` → bare `DatabaseError`
  ("disk damage — do not retry").
- `SnapshotFence` → `OperationalError`; `crates/lilliengine/src/py.rs` raises the
  dedicated `SnapshotFenceError` subclass for it.

`src/iai_mcp/hippo/_ro_pool.py` retries only on that fence class (structural
`isinstance`, falling back to the `"read-only snapshot invalidated"` message
marker). So before the fix, a reader that was merely *one generation behind* was
handed a corruption error, the fence-retry loop never matched it, and the read
failed instead of transparently retrying. The fix converts exactly that case into
the retryable class — which is the whole reason the distinction exists.

## Full-suite result

`cargo test -p lillibrain --release`, all 14 test binaries:

| Binary | Result |
|---|---|
| `lillibrain` (lib) | 64 passed |
| `btree_default_thresholds` | 2 passed |
| `btree_ops` | 27 passed |
| `btree_proptest` | 2 passed |
| `count_cache_txn` | 2 passed |
| `crash_delete_rebalance` | 2 passed |
| **`crash_recovery`** | **12 passed** — under the same parallel contention that failed pre-fix |
| `delete_borrow_overflow` | 1 passed |
| `delete_interior_byteful` | 1 passed |
| `integrity` | 9 passed |
| `key_fence` | 1 passed |
| `pager_basic` | 4 passed |
| `sidecar_durability` | 5 passed |
| `verbatim_blob` | 7 passed |
| `delete_sweep_stress` | pre-existing long-runner: 160k ops with per-op journal writes. **Measured 5,273.90 s (87.9 min)** on M2 Max release. Uses `Store::open` only (never a read-only pager), so `ro_snapshot_fence` is `None` and the pager change is a strict no-op for it. |

Method note worth keeping: the rate is roughly **0.5–2%**, so 6- and 14-run
samples cannot see it — the first pass called the test "stable" off six clean runs
and was wrong. Sample at **hundreds** of runs before calling anything here fixed.

Contention is *not* the trigger, and an earlier draft of this note claimed it was.
A sequential hammer (no added load, against the pre-fence-fix binary) reproduced
`PageOutOfBounds` at run **85**, while a 120-run sequential sample of the same
binary found none. That is one binomial process sampled twice, not a load effect;
concurrency only raises the odds per unit time. Sample size is the variable that
matters, so A/B a change by re-running the *failure-rate comparison* at hundreds
of iterations, not by reasoning about the failure mode.

The example probes are the durable artifact: each is re-runnable and prints the
number that justified the finding, so a fix can be verified against the same
measurement rather than against an assertion.

## Verification performed

| Check | Result |
|---|---|
| `cargo check --workspace --release` | clean (one pre-existing unused-import warning) |
| `cargo test -p lillibrain --release` (focused subsets) | pager_basic, integrity, key_fence, count_cache_txn, verbatim_blob, btree_ops, crash_recovery all pass |
| `cargo test -p lilli-hd --release` | 25/25 pass |
| `alloc_probe` before/after | the table in Finding 2 |
| Flaky test attributed | 14 runs pristine vs 14 with change |

