//! Cache-bound enforceability probe.
//!
//! `Pager::evict_if_over_budget_protecting` never evicts a DIRTY page
//! (correctness over budget). During an open write transaction every page the
//! transaction touched is dirty and is not cleared until commit, so the budget
//! can find no eviction candidate at all and the cache grows without bound.
//!
//! That matters because the batched-commit write path is exactly one big
//! transaction: the sleep pipeline's insert burst runs inside a single
//! `begin_write` / `commit`. This probe measures resident pages against the
//! configured bound inside and after the transaction.
//!
//! Run: cargo run --release -p lillibrain --example cache_bound_probe

use lillibrain::Store;

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
    p.push(format!("bound_probe_{name}_{}.lilli", std::process::id()));
    let _ = std::fs::remove_file(&p);
    p
}

fn main() {
    let rows: i64 = std::env::var("PROBE_N")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8000);
    // A deliberately tiny bound: if eviction worked, residency would stay near it.
    let bound: usize = 256;

    let path = tmp_path("bound");
    let store = Store::open(&path).expect("open");
    store.set_cache_pages(Some(bound));
    store.enable_wal_mode().expect("wal");
    let root = store.create_tree().expect("tree");

    println!("=== cache-bound enforceability probe ===");
    println!(
        "rows = {rows}, bound = {bound} pages ({bound} x {PAGE_SIZE} B = {} MiB)\n",
        bound * PAGE_SIZE / 1024 / 1024
    );

    store.begin_write().expect("begin");
    let mut marks: Vec<(i64, usize)> = Vec::new();
    for k in 0..rows {
        let rec = make_record(k);
        store.tree(root).insert(k, &rec).expect("insert");
        if k % 2000 == 0 || k == rows - 1 {
            marks.push((k + 1, store.pager().cached_len()));
        }
    }
    let live = store.pager().cached_len();

    println!("INSIDE the open transaction (every touched page is dirty):");
    for (rows_in, cached) in &marks {
        println!(
            "  after {rows_in:>6} rows: {cached:>6} pages resident  ({:>6}x the bound)",
            (*cached).div_ceil(bound)
        );
    }
    println!(
        "\n  resident {live} pages = {} MiB held in the page cache,",
        live * PAGE_SIZE / 1024 / 1024
    );
    println!("  despite a bound of {bound} pages. Eviction found no clean candidate.");

    store.commit().expect("commit");
    let after_commit = store.pager().cached_len();
    println!("\nAFTER commit (dirty cleared, eviction can finally run):");
    println!("  resident {after_commit} pages (bound {bound})");

    // The store's own data size for comparison.
    let db_pages = store.db_size().expect("db_size") as usize;
    println!(
        "\n  store size {db_pages} pages = {} MiB on disk",
        db_pages * PAGE_SIZE / 1024 / 1024
    );

    store.unlock().expect("unlock");
    let _ = std::fs::remove_file(&path);
}
