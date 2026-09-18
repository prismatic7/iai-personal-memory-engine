//! Allocation probe for the storage engine's read/commit/eviction paths.
//!
//! Measures *bytes allocated* (not wall time) so the per-operation memory
//! overhead is visible directly: a counting global allocator records how many
//! bytes each store call requests. This isolates footprint costs that timing
//! hides — a warm-cache read that allocates nothing still costs nothing, but a
//! warm-cache read that allocates two page buffers per call is pure waste.
//!
//! Run: cargo run --release -p lillibrain --example alloc_probe

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

use lillibrain::Store;

// --- counting allocator -----------------------------------------------------

static TOTAL_ALLOC: AtomicUsize = AtomicUsize::new(0);
static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);

struct Counting;

fn bump_live(size: usize) {
    let cur = LIVE.fetch_add(size, Ordering::Relaxed) + size;
    PEAK.fetch_max(cur, Ordering::Relaxed);
}

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = System.alloc(l);
        if !p.is_null() {
            TOTAL_ALLOC.fetch_add(l.size(), Ordering::Relaxed);
            bump_live(l.size());
        }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        LIVE.fetch_sub(l.size(), Ordering::Relaxed);
        System.dealloc(p, l);
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        let q = System.realloc(p, l, new);
        if !q.is_null() {
            if new > l.size() {
                TOTAL_ALLOC.fetch_add(new - l.size(), Ordering::Relaxed);
                bump_live(new - l.size());
            } else {
                LIVE.fetch_sub(l.size() - new, Ordering::Relaxed);
            }
        }
        q
    }
}

#[global_allocator]
static A: Counting = Counting;

fn total() -> usize {
    TOTAL_ALLOC.load(Ordering::Relaxed)
}
fn live() -> isize {
    LIVE.load(Ordering::Relaxed) as isize
}
fn peak() -> usize {
    PEAK.load(Ordering::Relaxed)
}
fn reset_peak() {
    PEAK.store(live().max(0) as usize, Ordering::Relaxed);
}

/// Bytes allocated while running `f`.
fn alloc_of<F: FnOnce()>(f: F) -> usize {
    let before = total();
    f();
    total() - before
}

// --- fixtures ---------------------------------------------------------------

/// Production record shape: 1536 B float32 embedding + ciphertext-sized tail.
const RECORD_LEN: usize = 1656;
const PAGE_SIZE: usize = 8192;

fn make_record(key: i64) -> Vec<u8> {
    let mut buf = vec![0u8; RECORD_LEN];
    buf[0..8].copy_from_slice(&key.to_be_bytes());
    for (i, b) in buf.iter_mut().enumerate().skip(8) {
        *b = (i as u8).wrapping_add(key as u8);
    }
    buf
}

fn tmp_path(name: &str) -> std::path::PathBuf {
    let mut p = std::env::temp_dir();
    let pid = std::process::id();
    p.push(format!("alloc_probe_{name}_{pid}.lilli"));
    let _ = std::fs::remove_file(&p);
    p
}

/// Build a store with `n` records in one batched write transaction.
fn build(n: i64, cache_pages: Option<usize>, name: &str) -> (Store, u32, std::path::PathBuf) {
    let path = tmp_path(name);
    let store = Store::open(&path).expect("open");
    store.set_cache_pages(cache_pages);
    store.enable_wal_mode().expect("wal");
    let root = store.create_tree().expect("tree");
    store.begin_write().expect("begin");
    for k in 0..n {
        let rec = make_record(k);
        store.tree(root).insert(k, &rec).expect("insert");
    }
    store.commit().expect("commit");
    store.checkpoint().expect("checkpoint");
    (store, root, path)
}

fn main() {
    let n: i64 = std::env::var("PROBE_N")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(6000);

    println!("=== lillibrain allocation probe (N = {n} records, {RECORD_LEN} B each) ===");
    println!("page size {PAGE_SIZE} B\n");

    // ---------------------------------------------------------------- probe 1
    // Warm-cache point read: the pager has every page resident, so a correct
    // read should allocate only the payload it returns.
    let (store, root, path) = build(n, Some(4096), "reads");
    let tree = store.tree(root);

    // Warm the cache with a scan, then measure a single point read.
    let _ = tree.get(n / 2).expect("warm get");
    let warm_get = alloc_of(|| {
        let _ = tree.get(n / 2).expect("get");
    });
    println!("[1] warm-cache point get ...... {warm_get:>7} B allocated per call");
    println!("    (payload is {RECORD_LEN} B; anything above that is pager overhead)");

    let mut keys: Vec<i64> = (0..64).map(|i| (i * 37) % n).collect();
    keys.sort_unstable();
    keys.dedup();
    let warm_many = alloc_of(|| {
        let _ = tree.get_many(&keys).expect("get_many");
    });
    println!(
        "[1] warm-cache get_many ({} keys) {warm_many:>7} B  ({:.0} B/key)",
        keys.len(),
        warm_many as f64 / keys.len() as f64
    );

    // ---------------------------------------------------------------- probe 2
    // Raw pager read on a fully warm cache: isolates the pager's own per-call
    // allocation from the payload copy.
    let pager = store.pager();
    let _ = pager.read_page(2).expect("warm page 2");
    let warm_page = alloc_of(|| {
        let _ = pager.read_page(2).expect("page 2");
    });
    let warm_page_scan = alloc_of(|| {
        let _ = pager.read_page_scan(2).expect("page 2 scan");
    });
    println!("[2] warm pager.read_page ...... {warm_page:>7} B allocated per call");
    println!("[2] warm pager.read_page_scan . {warm_page_scan:>7} B allocated per call");
    println!("    (page already resident — the scan path is the in-place pattern)");
    store.unlock().expect("unlock");
    let _ = std::fs::remove_file(&path);

    // ---------------------------------------------------------------- probe 3
    // Full scan: transient reads must not be inserted, so a scan should not
    // allocate one retained page per page walked.
    let (store2, root2, path2) = build(n, Some(4096), "scan");
    let tree2 = store2.tree(root2);
    let mut cells = 0usize;
    let scan_alloc = alloc_of(|| {
        tree2
            .scan_cells_with(|_k, _v| {
                cells += 1;
                Ok::<(), lillibrain::StoreError>(())
            })
            .expect("scan");
    });
    println!(
        "[3] streaming scan ({cells} cells) {scan_alloc:>7} B  ({:.0} B/cell)",
        scan_alloc as f64 / cells.max(1) as f64
    );
    store2.unlock().expect("unlock");
    let _ = std::fs::remove_file(&path2);

    // ---------------------------------------------------------------- probe 4
    // Commit: every dirty page is deep-cloned before it is written.
    let path4 = tmp_path("commit");
    let store4 = Store::open(&path4).expect("open");
    store4.set_cache_pages(Some(4096));
    store4.enable_wal_mode().expect("wal");
    let root4 = store4.create_tree().expect("tree");
    let batch: i64 = 2000;
    store4.begin_write().expect("begin");
    // Prime the cache so the measured commit sees a full dirty set.
    for k in 0..batch {
        let rec = make_record(k);
        store4.tree(root4).insert(k, &rec).expect("insert");
    }
    reset_peak();
    let live_before = live();
    let commit_alloc = alloc_of(|| {
        store4.commit().expect("commit");
    });
    let commit_peak = peak() as isize - live_before;
    println!(
        "[4] commit {batch} dirty pages ... {commit_alloc:>7} B allocated, peak +{commit_peak} B"
    );
    store4.unlock().expect("unlock");
    let _ = std::fs::remove_file(&path4);

    // ---------------------------------------------------------------- probe 5
    // Eviction overhead: the same insert burst with a small bounded cache
    // (which runs the eviction pass on every dirty write) vs an unbounded cache
    // (which runs no eviction at all). The difference is the eviction pass's
    // own allocation. The budget must be small relative to the page count the
    // burst produces, or the pass never runs and the comparison is a no-op.
    let burst: i64 = 3000;
    let small_cache: usize = 256;

    let run_burst = |cache: Option<usize>, name: &str| -> (usize, u64) {
        let path = tmp_path(name);
        let store = Store::open(&path).expect("open");
        store.set_cache_pages(cache);
        store.enable_wal_mode().expect("wal");
        let root = store.create_tree().expect("tree");
        store.begin_write().expect("begin");
        let a = alloc_of(|| {
            for k in 0..burst {
                let rec = make_record(k);
                store.tree(root).insert(k, &rec).expect("insert");
            }
        });
        let cached = store.pager().cached_len();
        store.rollback().expect("rollback");
        store.unlock().expect("unlock");
        let _ = std::fs::remove_file(&path);
        (a, cached as u64)
    };

    let (bounded, bounded_cached) = run_burst(Some(small_cache), "evict_bounded");
    let (unbounded, unbounded_cached) = run_burst(None, "evict_unbounded");

    println!(
        "[5] insert burst {burst} rows (~{} pages):",
        burst * RECORD_LEN as i64 / PAGE_SIZE as i64
    );
    println!("    bounded cache ({small_cache} pages, resident {bounded_cached}) {bounded:>12} B");
    println!("    unbounded cache (resident {unbounded_cached})               {unbounded:>12} B");
    println!(
        "    eviction pass overhead                        {:>12} B  ({} B/row)",
        bounded as isize - unbounded as isize,
        (bounded as isize - unbounded as isize) / burst as isize
    );

    // ---------------------------------------------------------------- probe 6
    // Descent cost: how many page reads one point lookup issues, and how much
    // of that is the warm-cache clone. The count is the tree-height proxy; the
    // bytes show the clone multiplier.
    let (store6, root6, path6) = build(n, Some(4096), "descent");
    let tree6 = store6.tree(root6);
    let _ = tree6.get(0).expect("warm");
    store6.reset_read_count();
    let _ = tree6.get(n / 2).expect("get");
    let reads = store6.read_count();
    println!(
        "[6] one point lookup ......... {reads} page reads ({reads} x 8192 B page buffers touched)"
    );
    store6.unlock().expect("unlock");
    let _ = std::fs::remove_file(&path6);
}
