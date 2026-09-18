//! Hypervector kernel allocation probe.
//!
//! The BSC/FHRR kernels are the hot path of the episodic and semantic tiers.
//! `permute` expands a packed hypervector to one byte PER BIT, rolls it, then
//! repacks — allocating 8x the payload plus the bit vector itself. On a 10000-bit
//! (1250-byte) hypervector that is a 10 KB unpacked bit array per call, which is
//! pure transient garbage on a per-binding path.
//!
//! The circular bit-roll is directly expressible in the packed domain, so this
//! probe measures what the unpack path actually costs to establish whether the
//! packed implementation is worth writing.
//!
//! Run: cargo run --release -p lilli-hd --example hv_probe

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

static TOTAL: AtomicUsize = AtomicUsize::new(0);

struct Counting;

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = System.alloc(l);
        if !p.is_null() {
            TOTAL.fetch_add(l.size(), Ordering::Relaxed);
        }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        System.dealloc(p, l);
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        let q = System.realloc(p, l, new);
        if !q.is_null() && new > l.size() {
            TOTAL.fetch_add(new - l.size(), Ordering::Relaxed);
        }
        q
    }
}

#[global_allocator]
static A: Counting = Counting;

fn main() {
    // The 10000-bit tier: 1250 packed bytes, matching HV_BYTES.
    let d: usize = 10000;
    let n_bytes = d.div_ceil(8);
    let hv: Vec<u8> = (0..n_bytes).map(|i| (i as u8).wrapping_mul(37)).collect();

    println!("=== hypervector kernel allocation probe (D = {d}, {n_bytes} packed bytes) ===");

    // permute: unpack(8x) + rolled copy + repack
    let before = TOTAL.load(Ordering::Relaxed);
    let out = lilli_hd::bsc::permute_impl(&hv, 7);
    let after = TOTAL.load(Ordering::Relaxed);
    let permute_alloc = after - before;
    println!(
        "[permute]      {permute_alloc:>8} B allocated for a {n_bytes} B hypervector  ({:.1}x the payload)",
        permute_alloc as f64 / n_bytes as f64
    );
    assert_eq!(out.len(), n_bytes);

    // bind: single output
    let before = TOTAL.load(Ordering::Relaxed);
    let _ = lilli_hd::bsc::bind_impl(&hv, &hv).expect("bind");
    let bind_alloc = TOTAL.load(Ordering::Relaxed) - before;
    println!(
        "[bind]         {bind_alloc:>8} B allocated  ({:.1}x the payload)",
        bind_alloc as f64 / n_bytes as f64
    );

    // hamming: popcount, should allocate nothing
    let before = TOTAL.load(Ordering::Relaxed);
    let _ = lilli_hd::bsc::hamming_impl(&hv, &hv);
    let hamming_alloc = TOTAL.load(Ordering::Relaxed) - before;
    println!("[hamming]      {hamming_alloc:>8} B allocated  (popcount path, zero-copy)");

    // bundle: the SWAR majority vote. Watch for a per-word plane allocation.
    let hvs: Vec<Vec<u8>> = (0..64).map(|_| hv.clone()).collect();
    let before = TOTAL.load(Ordering::Relaxed);
    let _ = lilli_hd::bsc::bundle_impl(&hvs, d).expect("bundle");
    let bundle_alloc = TOTAL.load(Ordering::Relaxed) - before;
    println!(
        "[bundle x64]   {bundle_alloc:>8} B allocated  ({:.1}x the payload); output plus internals",
        bundle_alloc as f64 / n_bytes as f64
    );

    // Per-call plane allocation inside bundle: one Vec<u64> per 8-byte word.
    let n_words = n_bytes / 8;
    println!("\n  bundle inner loop iterates {n_words} words; a per-word `vec![0u64; bit_width]`");
    println!(
        "  would be {} small allocations per call on top of the output.",
        n_words
    );
}
