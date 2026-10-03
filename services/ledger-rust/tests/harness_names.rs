//! The harness's own guarantee (fix wave 26b, CI #3 ledger-rust macos-26): two scratch paths made in the same
//! instant are distinct. Names were `label + pid + nanoseconds`; macOS's clock has microsecond resolution, so two
//! tests starting together with the same label (`PortFile::new("slow")`) shared one port file and one test talked to
//! the other's server (CI #3: `fifty_conflicting_posts…` read 184 entries, `eight_hundred_parallel_posts…` got seqs
//! 1..=200 because the other test's create took seq 0).
mod common;

use std::collections::HashSet;

#[test]
fn port_files_made_at_the_same_instant_from_many_threads_get_distinct_paths() {
    let handles: Vec<_> = (0..8)
        .map(|_| std::thread::spawn(|| (0..500).map(|_| common::PortFile::new("same")).collect::<Vec<_>>()))
        .collect();
    let files: Vec<common::PortFile> = handles.into_iter().flat_map(|h| h.join().unwrap()).collect();
    let paths: HashSet<_> = files.iter().map(|f| f.0.clone()).collect();
    assert_eq!(paths.len(), files.len(), "port files with the same label collided");
}

#[test]
fn unique_suffixes_never_repeat_within_a_process() {
    let handles: Vec<_> = (0..8)
        .map(|_| std::thread::spawn(|| (0..500).map(|_| common::unique_suffix()).collect::<Vec<_>>()))
        .collect();
    let all: Vec<String> = handles.into_iter().flat_map(|h| h.join().unwrap()).collect();
    let set: HashSet<_> = all.iter().cloned().collect();
    assert_eq!(set.len(), all.len());
}

#[test]
fn every_scratch_path_lives_in_one_per_process_directory() {
    // Scout C C1-1: the tests wrote `ledger_*` / `ledger_port_*` files straight into the temp dir, so a run killed
    // outside the hygiene wrapper left many top-level temp entries. Every scratch path is now inside one per-process
    // directory (made on first use, removed at exit when empty — a file left in it is a real leak, which the wrapper's
    // R3 reports), so a killed run leaves at most that one entry.
    let dir = common::scratch_dir();
    assert_eq!(dir.parent(), Some(common::real_temp_dir().as_path()));
    assert!(dir.file_name().unwrap().to_string_lossy().starts_with("zbm-ledger-tests-"), "{dir:?}");
    assert!(dir.is_dir());
    assert_eq!(common::scratch_dir(), dir, "one directory per process");
    let pf = common::PortFile::new("in-dir");
    assert_eq!(pf.0.parent(), Some(dir.as_path()));
}
