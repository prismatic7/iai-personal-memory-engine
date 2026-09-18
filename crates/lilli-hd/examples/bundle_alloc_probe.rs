//! Allocation attribution for the BSC majority-vote bundle.
//!
//! `bundle_impl` accumulates per-bit-position counts in a bit-sliced SWAR
//! ladder. The ladder (`planes: Vec<u64>`, one plane per count bit) is
//! re-allocated once per 64-bit word of the hypervector — on a 10000-bit
//! hypervector that is 156 allocations per call, all of them transient and all
//! of them avoidable, because `planes.len()` is bounded by
//! `ceil(log2(len(bound_hvs)))` and the width is known before the loop starts.
//!
//! This probe separates the output allocation from the per-word ladder churn so
//! the fix's headroom is a number rather than an estimate.
//!
//! Run: cargo run --release -p lilli-hd --example bundle_alloc_probe

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

static TOTAL: AtomicUsize = AtomicUsize::new(0);
static CALLS: AtomicUsize = AtomicUsize::new(0);

struct Counting;

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        let p = System.alloc(l);
        if !p.is_null() {
            TOTAL.fetch_add(l.size(), Ordering::Relaxed);
            CALLS.fetch_add(1, Ordering::Relaxed);
        }
        p
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        System.dealloc(p, l);
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, new: usize) -> *mut u8 {
        let q = System.realloc(p, l, new);
        if !q.is_null() {
            TOTAL.fetch_add(new, Ordering::Relaxed);
            CALLS.fetch_add(1, Ordering::Relaxed);
        }
        q
    }
}

#[global_allocator]
static A: Counting = Counting;

fn main() {
    let d: usize = 10000;
    let n_bytes = d.div_ceil(8);
    let width = 100; // hypervectors bundled per call

    let one: Vec<u8> = (0..n_bytes).map(|i| (i as u8).wrapping_mul(37)).collect();
    let hvs: Vec<Vec<u8>> = (0..width).map(|_| one.clone()).collect();

    println!("=== bundle allocation attribution (D = {d}, {width} inputs) ===");
    let n_words = n_bytes / 8;
    let bit_width = (u32::BITS - (width as u32).leading_zeros()).max(1) as usize;
    println!("  output = {n_bytes} B; inner loop = {n_words} words x {bit_width}-plane ladder");

    let b0 = TOTAL.load(Ordering::Relaxed);
    let c0 = CALLS.load(Ordering::Relaxed);
    let out = lilli_hd::bsc::bundle_impl(&hvs, d).expect("bundle");
    let bytes = TOTAL.load(Ordering::Relaxed) - b0;
    let calls = CALLS.load(Ordering::Relaxed) - c0;
    assert_eq!(out.len(), n_bytes);

    println!("\n  total allocated      {bytes} B");
    println!("  allocation calls     {calls}");
    println!("  unavoidable (output) {n_bytes} B in 1 call");
    println!(
        "  ladder churn         ~{} B in ~{} calls  ({:.1}x the output)",
        bytes - n_bytes,
        calls.saturating_sub(2),
        (bytes - n_bytes) as f64 / n_bytes as f64
    );
    println!(
        "\n  A hoisted ladder (allocated once before the word loop) plus a reused\n  \
         scratch buffer removes the {n_words} per-word allocations entirely:\n  \
         expected residual = {n_bytes} B (output) + {bit_width} x 8 B (one ladder)."
    );
}
