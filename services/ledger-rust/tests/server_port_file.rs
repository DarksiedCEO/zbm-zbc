//! Fix wave 22 (AEGIS N21-C-2, lead ruling G7): LEDGER_PORT_FILE.
//!
//! The port file used to be written as `<path>.tmp-<pid>` (a predictable name, followed if it was a symlink, so a
//! planted link let the server overwrite any file the ledger's user can write), BEFORE the ledger log was opened
//! (a server that then refused to start left a port file naming a dead port), with the default mode, and it was
//! never removed. Now: the temp file is created O_CREAT|O_EXCL|O_NOFOLLOW with a random suffix in the target's
//! directory, mode 0600; a target that is a symlink is refused; it is written only after the ledger opened, renamed
//! atomically into place, and removed on a clean exit and on SIGTERM/SIGINT (only while it is still the file the
//! server wrote).
#![cfg(unix)]

mod common;

use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use common::{wait_port, PortFile};

struct Scratch(PathBuf);

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn scratch(label: &str) -> Scratch {
    let d = common::real_temp_dir().join(format!("ledger_pf_{label}_{}", common::unique_suffix()));
    std::fs::create_dir_all(&d).unwrap();
    Scratch(d)
}

/// The server process, killed and reaped when the test ends however it ends (a failed assertion included): a test
/// never leaves a server running.
struct Srv(Child);

impl Drop for Srv {
    fn drop(&mut self) {
        if let Ok(None) = self.0.try_wait() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

fn server(dir: &Path, pf: &Path, log: &Path) -> Command {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_server"));
    cmd.env("LEDGER_SERVICE_TOKEN", "port-file-test-token-0123456789")
        .env("LEDGER_PORT", "0")
        .env("LEDGER_PORT_FILE", pf)
        .env("LEDGER_LOG_PATH", log)
        .current_dir(dir)
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    cmd
}

fn wait_exit(child: &mut Child, limit: Duration) -> Option<std::process::ExitStatus> {
    let deadline = Instant::now() + limit;
    loop {
        if let Some(st) = child.try_wait().unwrap() {
            return Some(st);
        }
        if Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn term(child: &Child) {
    let st = Command::new("kill").arg("-TERM").arg(child.id().to_string()).status().unwrap();
    assert!(st.success());
}

#[test]
fn a_symlink_planted_at_the_predictable_temp_name_is_never_followed() {
    // `sh -c '... exec server'`: the server keeps the shell's pid, so the old `<path>.tmp-<pid>` name is known
    // before the server runs and a link can be planted there — exactly the attack.
    let s = scratch("tmp_link");
    let victim = s.0.join("victim.txt");
    std::fs::write(&victim, b"precious\n").unwrap();
    let pf = PortFile(s.0.join("ledger.port"));
    let log = s.0.join("ledger.jsonl");
    let mut cmd = Command::new("sh");
    cmd.arg("-c")
        .arg("ln -s \"$VICTIM\" \"$LEDGER_PORT_FILE.tmp-$$\" && exec \"$SERVER\"")
        .env("VICTIM", &victim)
        .env("SERVER", env!("CARGO_BIN_EXE_server"))
        .env("LEDGER_SERVICE_TOKEN", "port-file-test-token-0123456789")
        .env("LEDGER_PORT", "0")
        .env("LEDGER_PORT_FILE", &pf.0)
        .env("LEDGER_LOG_PATH", &log)
        .current_dir(&s.0)
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let mut srv = Srv(cmd.spawn().unwrap());
    let child = &mut srv.0;
    let port = wait_port(child, &pf, Duration::from_secs(20));
    let victim_after = std::fs::read(&victim).unwrap();
    term(child);
    let _ = wait_exit(child, Duration::from_secs(10));
    assert_eq!(victim_after, b"precious\n", "the planted link was followed: the victim now holds {victim_after:?}");
    assert!(port.is_ok(), "{port:?}");
}

#[test]
fn a_symlink_at_the_port_file_path_is_refused() {
    let s = scratch("target_link");
    let victim = s.0.join("victim.txt");
    std::fs::write(&victim, b"precious\n").unwrap();
    let pf = s.0.join("ledger.port");
    std::os::unix::fs::symlink(&victim, &pf).unwrap();
    let mut srv = Srv(server(&s.0, &pf, &s.0.join("ledger.jsonl")).spawn().unwrap());
    let child = &mut srv.0;
    let st = wait_exit(child, Duration::from_secs(20)).expect("the server kept running with a symlinked port file");
    assert!(!st.success(), "{st}");
    assert_eq!(std::fs::read(&victim).unwrap(), b"precious\n");
    assert!(std::fs::symlink_metadata(&pf).unwrap().file_type().is_symlink(), "the link itself was replaced");
    let mut err = String::new();
    std::io::Read::read_to_string(child.stderr.as_mut().unwrap(), &mut err).unwrap();
    assert!(err.contains("symlink"), "{err}");
}

#[test]
fn the_port_file_is_written_only_after_the_ledger_opened() {
    // a log that fails verification: the server refuses to start — and must not have announced a port
    let s = scratch("bad_log");
    let log = s.0.join("ledger.jsonl");
    std::fs::write(&log, b"{\"not\": \"a ledger entry\"}\n").unwrap();
    let pf = s.0.join("ledger.port");
    let mut srv = Srv(server(&s.0, &pf, &log).spawn().unwrap());
    let child = &mut srv.0;
    let st = wait_exit(child, Duration::from_secs(20)).expect("the server started on a corrupt log");
    assert!(!st.success(), "{st}");
    assert!(!pf.exists(), "a port file was left for a server that never served: {:?}", std::fs::read_to_string(&pf));
    let left: Vec<_> = std::fs::read_dir(&s.0).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    assert!(left.iter().all(|n| !n.contains(".tmp")), "{left:?}");
}

#[test]
fn a_stale_port_file_is_replaced_the_new_one_is_0600_and_sigterm_removes_it() {
    let s = scratch("stale");
    let pf = PortFile(s.0.join("ledger.port"));
    std::fs::write(&pf.0, b"1\n").unwrap();                     // a previous run's (a crash left it)
    std::fs::set_permissions(&pf.0, std::fs::Permissions::from_mode(0o644)).unwrap();
    let mut srv = Srv(server(&s.0, &pf.0, &s.0.join("ledger.jsonl")).spawn().unwrap());
    let child = &mut srv.0;
    // the stale file is readable until the server replaces it (a caller removes it first, as PortFile::new does);
    // what matters: it is REPLACED (atomically), by a private file
    let deadline = Instant::now() + Duration::from_secs(20);
    let port = loop {
        match pf.read() {
            Some(p) if p != 1 => break p,
            _ if Instant::now() > deadline => panic!("the stale port file was never replaced"),
            _ => std::thread::sleep(Duration::from_millis(20)),
        }
    };
    assert!(port > 1);
    let meta = std::fs::symlink_metadata(&pf.0).unwrap();
    assert!(meta.file_type().is_file());
    assert_eq!(meta.mode() & 0o777, 0o600, "mode {:o}", meta.mode() & 0o777);
    let left: Vec<_> = std::fs::read_dir(&s.0).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    assert!(left.iter().all(|n| !n.contains(".tmp")), "{left:?}");
    term(child);
    let st = wait_exit(child, Duration::from_secs(10)).expect("SIGTERM did not stop the server");
    assert!(!st.success());                                    // killed by the signal, as before
    assert!(!pf.0.exists(), "the port file outlived the server");
}

#[test]
fn sigterm_leaves_a_port_file_that_is_no_longer_the_servers_own() {
    // someone else's file at the path by the time the server stops (a newer server, say): not the server's to remove
    let s = scratch("replaced");
    let pf = PortFile(s.0.join("ledger.port"));
    let mut srv = Srv(server(&s.0, &pf.0, &s.0.join("ledger.jsonl")).spawn().unwrap());
    let child = &mut srv.0;
    wait_port(child, &pf, Duration::from_secs(20)).unwrap();
    let other = s.0.join("other.port");
    std::fs::write(&other, b"4242\n").unwrap();
    std::fs::rename(&other, &pf.0).unwrap();
    term(child);
    wait_exit(child, Duration::from_secs(10)).expect("SIGTERM did not stop the server");
    assert_eq!(std::fs::read(&pf.0).unwrap(), b"4242\n");
}

// ---- fix wave 23 (AEGIS round 22, N22-C-3): the PARENT of the port file -----------------------------------------

fn names(dir: &Path) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(dir).unwrap().map(|e| e.unwrap().file_name().into_string().unwrap()).collect();
    v.sort();
    v
}

fn stderr_of(child: &mut Child) -> String {
    let mut err = String::new();
    std::io::Read::read_to_string(child.stderr.as_mut().unwrap(), &mut err).unwrap();
    err
}

#[test]
fn a_port_file_whose_parent_directory_is_a_symlink_is_refused_and_the_file_behind_it_is_untouched() {
    // the reviewers' g7_dirlink: LEDGER_PORT_FILE=<dir>/linkdir/p3.port where linkdir -> victimdir holding a regular
    // p3.port. Before: the server replaced victimdir/p3.port (0600, its port) and SIGTERM then deleted it.
    let s = scratch("dir_link");
    let victim_dir = s.0.join("victimdir");
    std::fs::create_dir(&victim_dir).unwrap();
    let victim = victim_dir.join("p3.port");
    std::fs::write(&victim, b"VICTIM-DIR-FILE\n").unwrap();
    let link = s.0.join("linkdir");
    std::os::unix::fs::symlink(&victim_dir, &link).unwrap();
    let pf = link.join("p3.port");
    let mut srv = Srv(server(&s.0, &pf, &s.0.join("ledger.jsonl")).spawn().unwrap());
    let child = &mut srv.0;
    let st = wait_exit(child, Duration::from_secs(20)).expect("the server kept running with its port file behind a directory symlink");
    assert!(!st.success(), "{st}");
    assert_eq!(std::fs::read(&victim).unwrap(), b"VICTIM-DIR-FILE\n");
    assert_eq!(std::fs::metadata(&victim).unwrap().mode() & 0o777, 0o644);
    assert_eq!(names(&victim_dir), vec!["p3.port".to_string()]);
    let err = stderr_of(child);
    assert!(err.contains("symlink") && err.contains("linkdir"), "{err}");
}

#[test]
fn a_symlink_anywhere_in_the_parent_path_is_refused() {
    // real/a/ holds the target directory; hop -> real; LEDGER_PORT_FILE=<dir>/hop/a/ledger.port
    let s = scratch("deep_link");
    let deep = s.0.join("real").join("a");
    std::fs::create_dir_all(&deep).unwrap();
    std::os::unix::fs::symlink(s.0.join("real"), s.0.join("hop")).unwrap();
    let pf = s.0.join("hop").join("a").join("ledger.port");
    let mut srv = Srv(server(&s.0, &pf, &s.0.join("ledger.jsonl")).spawn().unwrap());
    let child = &mut srv.0;
    let st = wait_exit(child, Duration::from_secs(20)).expect("the server kept running with a symlink in its port file's parent path");
    assert!(!st.success(), "{st}");
    assert!(names(&deep).is_empty(), "{:?}", names(&deep));
    let err = stderr_of(child);
    assert!(err.contains("symlink") && err.contains("hop"), "{err}");
}

#[test]
fn a_relative_port_file_in_a_real_directory_still_works() {
    let s = scratch("relative");
    std::fs::create_dir(s.0.join("sub")).unwrap();
    let mut srv = Srv(server(&s.0, Path::new("sub/ledger.port"), &s.0.join("ledger.jsonl")).spawn().unwrap());
    let child = &mut srv.0;
    let pf = PortFile(s.0.join("sub").join("ledger.port"));
    let port = wait_port(child, &pf, Duration::from_secs(20));
    assert!(port.is_ok(), "{port:?}");
    term(child);
    wait_exit(child, Duration::from_secs(10)).expect("SIGTERM did not stop the server");
    assert!(!pf.0.exists());
}

#[test]
fn sighup_and_sigquit_remove_the_port_file_as_sigterm_does() {
    for sig in ["-HUP", "-QUIT"] {
        let s = scratch(&format!("sig{}", &sig[1..]));
        let pf = PortFile(s.0.join("ledger.port"));
        let mut srv = Srv(server(&s.0, &pf.0, &s.0.join("ledger.jsonl")).spawn().unwrap());
        let child = &mut srv.0;
        wait_port(child, &pf, Duration::from_secs(20)).unwrap();
        let ok = Command::new("kill").arg(sig).arg(child.id().to_string()).status().unwrap();
        assert!(ok.success());
        let st = wait_exit(child, Duration::from_secs(10)).unwrap_or_else(|| panic!("{sig} did not stop the server"));
        assert!(!st.success(), "{sig}: {st}");                 // still killed by the signal (default disposition re-raised)
        assert!(!pf.0.exists(), "{sig}: the port file outlived the server");
    }
}

/// Fix wave 24, F2 (AEGIS N23-S-2): a stop signal aimed at the publish window. The handlers used to be installed
/// only AFTER the rename, so a SIGTERM that arrived while the temp file existed killed the server with the default
/// disposition and left `.<name>.tmp-<hex>` behind (AEGIS: 297/300), and one that arrived between the rename and
/// the handlers left the port file. Now the stop signals are blocked across the whole publish and the handlers are
/// installed before anything is written; the handler removes the temp name and the published name. Fired the
/// moment the temp name (`T`) or the port file (`P`) appears; nothing may be left either way.
#[test]
fn a_stop_signal_aimed_at_the_publish_window_leaves_neither_the_temp_file_nor_the_port_file() {
    const N: usize = 100;
    let mut report = Vec::new();
    for mode in ['T', 'P'] {
        // Fix wave 25 (scout C2-6): the sample is N signals that landed IN the window, however many spawns that takes
        // (at most 4N). The old `hit >= 90% of N spawns` failed a correct server whenever a loaded box made the
        // poller miss the window, which says nothing about the server.
        let (mut hit, mut stale, mut spawned) = (0usize, Vec::new(), 0usize);
        for k in 0..4 * N {
            if hit == N {
                break;
            }
            spawned += 1;
            let s = scratch(&format!("aim{mode}{k}"));
            let pf = PortFile(s.0.join("x.port"));
            let mut srv = Srv(server(&s.0, &pf.0, &s.0.join("l.jsonl")).stderr(Stdio::null()).spawn().unwrap());
            let child = &mut srv.0;
            let deadline = Instant::now() + Duration::from_secs(10);
            while Instant::now() < deadline && child.try_wait().unwrap().is_none() {
                let ns = names(&s.0);
                let (tmp, port) = (ns.iter().any(|n| n.contains(".tmp-")), ns.iter().any(|n| n == "x.port"));
                if (mode == 'T' && tmp) || port {
                    // SAFETY: signalling the child this test spawned (still unreaped: its pid is not reused).
                    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM) };
                    hit += usize::from(mode == 'P' || tmp);  // mode T: a miss when only the port file was seen
                    break;
                }
            }
            wait_exit(child, Duration::from_secs(10)).expect("the server did not stop");
            let left: Vec<String> = names(&s.0).into_iter().filter(|n| n.contains(".tmp-") || n == "x.port").collect();
            if !left.is_empty() {
                stale.push(format!("#{k}: {left:?}"));
            }
        }
        report.push(format!("mode {mode}: spawned={spawned} signalled_in_window={hit} left_behind={} {:?}", stale.len(),
                            stale.iter().take(3).collect::<Vec<_>>()));
        assert_eq!(hit, N, "only {hit} of {spawned} spawns were signalled inside the window: {report:?}");
    }
    eprintln!("{}", report.join("\n"));
    assert!(report.iter().all(|r| r.contains("left_behind=0 ")), "{report:#?}");
}
