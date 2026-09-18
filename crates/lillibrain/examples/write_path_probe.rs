//! Write-path working-set probe.
//!
//! The pager's eviction refuses to evict a DIRTY page ("correctness over
//! budget"). In an open transaction every touched page is dirty until commit,
//! so the bound is unenforceable exactly when a write burst runs — the case the
//! bound exists to cover. `cache_bound_probe` measures that residency blowup.
//!
//! This probe answers the follow-on question a fix needs: does residency track
//! the *data* written (unavoidable) or the *access pattern* (avoidable)? A
//! sequential append's live working set is O(1) pages — one rightmost leaf plus
//! the interior path — so any residency growth beyond that is pages the cache
//! is holding without need.
//!
//! Run: cargo run --release -p lillibrain --example write_path_probe

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
    p.push(format!("wp_probe_{name}_{}.lilli", std::process::id()));
    let _ = std::fs::remove_file(&p);
    p
}

fn main() {
    let rows: i64 = std::env::var("PROBE_N")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8000);

    // ---- A. sequential append: the sleep pipeline's shape -------------------
    {
        let path = tmp_path("seq");
        let store = Store::open(&path).expect("open");
        store.enable_wal_mode().expect("wal");
        let root = store.create_tree().expect("tree");
        store.set_cache_pages(Some(64));
        store.begin_write().expect("begin");

        let mut marks = Vec::new();
        for k in 0..rows {
            let rec = make_record(k);
            store.tree(root).insert(k, &rec).expect("insert");
            if (k + 1) % 2000 == 0 || k == rows - 1 {
                marks.push((k + 1, store.pager().cached_len()));
            }
        }
        let db_pages = store.db_size().expect("db_size") as usize;

        println!("=== A. sequential append (ascending keys, rightmost-leaf growth) ===");
        println!(
            "  bound 64 pages ({:} KiB). Resident pages as rows accumulate:",
            64 * PAGE_SIZE / 1024
        );
        for (n, cached) in &marks {
            println!("    {n:>6} rows -> {cached:>6} resident");
        }
        println!(
            "  db_size {db_pages} pages; residency {:.0}% of the file",
            marks.last().unwrap().1 as f64 / db_pages as f64 * 100.0
        );
        println!("  The live working set for this access pattern is O(1) pages:");
        println!("  every insert descends the same interior path to the same leaf.");
        store.rollback().expect("rollback");
        store.unlock().expect("unlock");
        let _ = std::fs::remove_file(&path);
    }

    // ---- B. random update: residency vs distinct rows touched ---------------
    {
        let path = tmp_path("rand");
        let store = Store::open(&path).expect("open");
        store.enable_wal_mode().expect("wal");
        let root = store.create_tree().expect("tree");
        store.set_cache_pages(None);
        store.begin_write().expect("begin");
        for k in 0..rows {
            let rec = make_record(k);
            store.tree(root).insert(k, &rec).expect("insert");
        }
        store.commit().expect("commit");
        let db_pages = store.db_size().expect("db_size") as usize;

        let updates = rows / 4;
        store.begin_write().expect("begin");
        let mut lcg: u64 = 0x2545F4914F6CDD1D;
        for _ in 0..updates {
            lcg = lcg
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            let k = ((lcg >> 33) % rows as u64) as i64;
            let rec = make_record(k);
            store.tree(root).insert(k, &rec).expect("insert");
        }
        let resident = store.pager().cached_len();

        println!("\n=== B. random-order update ({updates} of {rows} rows, one txn) ===");
        println!(
            "  db_size   {db_pages} pages ({} MiB)",
            db_pages * PAGE_SIZE / 1024 / 1024
        );
        println!(
            "  resident  {resident} pages ({} MiB)",
            resident * PAGE_SIZE / 1024 / 1024
        );
        println!(
            "  residency is {:.0}% of the file after updating {:.0}% of the rows.",
            resident as f64 / db_pages as f64 * 100.0,
            updates as f64 / rows as f64 * 100.0
        );
        println!("  A random quarter touches most leaves, so here residency IS the");
        println!("  data set — the avoidable case is pattern A, not this one.");
        store.rollback().expect("rollback");
        store.unlock().expect("unlock");
        let _ = std::fs::remove_file(&path);
    }
}
