//! Daemon self-update (SF-026): a configured, durable handoff to a new daemon
//! binary, with startup fallback that never relies on the new binary.
//!
//! The service manager starts the platform-installed base binary with
//! `--launch`. The launcher reads `handoff.json` in the install directory and
//! `exec`s (same PID, so readiness and watchdog stay with the service manager)
//! one of:
//! - a pending candidate, after durably counting the attempt;
//! - the last confirmed binary (`current`);
//! - itself, the base binary, when nothing else is confirmed or startable.
//!
//! A candidate confirms itself only after a complete local startup. If it
//! exits, fails to execute, or never signals readiness (the service manager's
//! start timeout or watchdog kills it), the next start finds an unconfirmed
//! attempt; after `max_start_attempts` the launcher rolls back to the previous
//! binary, which reports the deployment Failed. SuiFleet never reboots, writes
//! a bootloader, or replaces the platform's base binary: system reboot, boot
//! confirmation and OS rollback stay platform-owned.
use crate::config::DaemonUpdateConfig;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

/// Exit status asking the service manager to restart into the handoff.
pub const RESTART_EXIT: i32 = 75;
/// Environment variable the launcher sets for a candidate it starts.
pub const CANDIDATE_ENV: &str = "SUIFLEET_HANDOFF";
const STATE: &str = "handoff.json";
const CURRENT: &str = "current";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Phase {
    /// Staged and preflighted; the launcher may start it.
    Pending,
    /// The candidate completed startup and is now `current`.
    Confirmed,
    /// The candidate did not start; the launcher went back to the previous binary.
    RolledBack,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Handoff {
    pub deployment: String,
    pub version: String,
    pub candidate: PathBuf,
    /// The confirmed binary before this handoff (None: the base binary).
    pub previous: Option<PathBuf>,
    pub attempts: u32,
    pub phase: Phase,
    pub error: Option<String>,
}

/// Raised by staging: the deployment stays pending until the handoff resolves,
/// and the daemon exits with [`RESTART_EXIT`].
#[derive(Debug)]
pub struct Requested;
impl std::fmt::Display for Requested {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("daemon update staged; restarting into the new daemon")
    }
}
impl std::error::Error for Requested {}

pub fn install_dir(config: &DaemonUpdateConfig, state_dir: &Path) -> PathBuf {
    config.install_dir.clone().unwrap_or_else(|| state_dir.join("daemon"))
}

pub fn read(dir: &Path) -> anyhow::Result<Option<Handoff>> {
    match std::fs::read(dir.join(STATE)) {
        Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub fn write(dir: &Path, handoff: &Handoff) -> anyhow::Result<()> {
    atomic_write(&dir.join(STATE), &serde_json::to_vec_pretty(handoff)?)
}

fn clear(dir: &Path) -> anyhow::Result<()> {
    match std::fs::remove_file(dir.join(STATE)) {
        Ok(()) => sync_dir(dir),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

pub fn current(dir: &Path) -> Option<PathBuf> {
    let text = std::fs::read_to_string(dir.join(CURRENT)).ok()?;
    let path = PathBuf::from(text.trim());
    path.is_absolute().then_some(path)
}

fn set_current(dir: &Path, path: Option<&Path>) -> anyhow::Result<()> {
    match path {
        Some(path) => atomic_write(&dir.join(CURRENT), path.to_string_lossy().as_bytes()),
        None => match std::fs::remove_file(dir.join(CURRENT)) {
            Ok(()) => sync_dir(dir),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        },
    }
}

// --- Launcher ----------------------------------------------------------------

/// What the launcher starts.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Start {
    /// A pending candidate (the handoff's attempt count is already updated).
    Candidate(PathBuf),
    /// A confirmed earlier update.
    Confirmed(PathBuf),
    /// The launcher's own (base) binary.
    Base,
}

/// The launcher's decision for one start, and the handoff state to persist
/// before acting on it. Pure, so every transition is unit-tested.
pub fn decide(handoff: Option<&Handoff>, current: Option<&Path>, max_attempts: u32) -> (Start, Option<Handoff>) {
    let fallback = |current: Option<&Path>| current.map(|p| Start::Confirmed(p.to_owned())).unwrap_or(Start::Base);
    let Some(handoff) = handoff else {
        return (fallback(current), None);
    };
    if handoff.phase != Phase::Pending {
        return (fallback(current), None);
    }
    if handoff.attempts >= max_attempts {
        let mut failed = handoff.clone();
        failed.phase = Phase::RolledBack;
        failed.error = Some(format!(
            "new daemon {} did not complete startup in {} attempt(s); restarted the previous daemon",
            handoff.version, handoff.attempts
        ));
        return (fallback(handoff.previous.as_deref()), Some(failed));
    }
    let mut attempt = handoff.clone();
    attempt.attempts += 1;
    (Start::Candidate(handoff.candidate.clone()), Some(attempt))
}

/// `suifleet --launch`: pick the binary for this start and `exec` it. Returns
/// only when the base binary should run in this process.
pub fn launch(config: &DaemonUpdateConfig, state_dir: &Path) -> anyhow::Result<()> {
    if !config.enabled {
        return Ok(());
    }
    let dir = install_dir(config, state_dir);
    std::fs::create_dir_all(&dir)?;
    let handoff = read(&dir).unwrap_or_else(|error| {
        eprintln!("suifleet launcher: unreadable {STATE} ({error:#}); starting the confirmed daemon");
        None
    });
    let confirmed = current(&dir);
    let (start, update) = decide(handoff.as_ref(), confirmed.as_deref(), config.max_start_attempts);
    if let Some(update) = &update {
        // Durable before acting: a crash during this start still counts.
        write(&dir, update)?;
    }
    let mut start = start;
    loop {
        match &start {
            Start::Base => return Ok(()),
            Start::Candidate(path) | Start::Confirmed(path) => {
                let error = exec(path, matches!(start, Start::Candidate(_)).then(|| handoff.as_ref().unwrap().deployment.as_str()));
                // exec returned: the binary cannot even be executed.
                eprintln!("suifleet launcher: cannot execute {} ({error})", path.display());
                start = match start {
                    Start::Candidate(_) => {
                        let mut failed = update.clone().unwrap();
                        failed.phase = Phase::RolledBack;
                        failed.error = Some(format!("new daemon {} cannot be executed: {error}", failed.version));
                        write(&dir, &failed)?;
                        failed.previous.map(Start::Confirmed).unwrap_or(Start::Base)
                    }
                    _ => Start::Base,
                };
            }
        }
    }
}

#[cfg(unix)]
fn exec(path: &Path, candidate: Option<&str>) -> std::io::Error {
    use std::os::unix::process::CommandExt;
    let mut command = std::process::Command::new(path);
    command.env_remove(CANDIDATE_ENV);
    if let Some(deployment) = candidate {
        command.env(CANDIDATE_ENV, deployment);
    }
    command.exec()
}
#[cfg(not(unix))]
fn exec(_: &Path, _: Option<&str>) -> std::io::Error {
    std::io::Error::other("handoff requires Unix exec")
}

// --- Staging -------------------------------------------------------------------

/// Install a verified daemon binary, preflight it against this device's
/// configuration, identity and journal, and record a pending handoff.
pub async fn stage(
    config: &DaemonUpdateConfig,
    state_dir: &Path,
    config_path: &Path,
    deployment: &str,
    version: &str,
    artifact: &Path,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        !version.is_empty() && version.len() <= 64 && version.bytes().all(|b| b.is_ascii_alphanumeric() || b"._+-".contains(&b)),
        "daemon version is not a safe path segment"
    );
    let dir = install_dir(config, state_dir);
    if let Some(existing) = read(&dir)? {
        anyhow::ensure!(existing.phase != Phase::Pending, "another daemon update ({}) is still pending", existing.deployment);
    }
    let tag = hex::encode(&crate::identity::hash(deployment.as_bytes())[..4]);
    let version_dir = dir.join("versions").join(format!("{version}-{tag}"));
    let _ = std::fs::remove_dir_all(&version_dir);
    std::fs::create_dir_all(&version_dir)?;
    let candidate = version_dir.join("suifleet");
    std::fs::copy(artifact, &candidate)?;
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&candidate, std::fs::Permissions::from_mode(0o755))?;
    }
    std::fs::File::open(&candidate)?.sync_all()?;
    sync_dir(&version_dir)?;
    // The candidate must accept this device's configuration, identity and
    // journal before the running daemon gives way to it.
    let mut check = tokio::process::Command::new(&candidate);
    check.arg("--self-check").arg(config_path);
    let preflight = crate::apply::run_bounded(
        check,
        &version_dir,
        std::time::Duration::from_secs(config.preflight_timeout_seconds),
        None,
    )
    .await;
    if let Err(error) = preflight {
        let _ = std::fs::remove_dir_all(&version_dir);
        return Err(error.context("new daemon failed its preflight self-check; the running daemon is unchanged"));
    }
    write(&dir, &Handoff {
        deployment: deployment.to_owned(),
        version: version.to_owned(),
        candidate,
        previous: current(&dir),
        attempts: 0,
        phase: Phase::Pending,
        error: None,
    })?;
    Ok(())
}

// --- Resolution ----------------------------------------------------------------

/// How this daemon's startup resolves a recorded handoff.
#[derive(Debug, PartialEq, Eq)]
pub enum Resolution {
    /// Nothing recorded.
    None,
    /// This process is the pending candidate: confirm after a complete startup.
    ConfirmAfterStartup(Handoff),
    /// The deployment applied (confirmed earlier, maybe before a crash).
    Applied(Handoff),
    /// The deployment failed (rolled back, or pending without the launcher).
    Failed(Handoff, String),
}

/// Decide at startup, before deployments are processed.
pub fn resolve(dir: &Path, candidate_env: Option<&str>) -> anyhow::Result<Resolution> {
    let Some(handoff) = read(dir)? else {
        return Ok(Resolution::None);
    };
    Ok(match handoff.phase {
        Phase::Pending if candidate_env == Some(handoff.deployment.as_str()) => Resolution::ConfirmAfterStartup(handoff),
        Phase::Pending => {
            let reason = format!(
                "daemon update {} was staged but this daemon was not started by `suifleet --launch` as its candidate",
                handoff.version
            );
            Resolution::Failed(handoff, reason)
        }
        Phase::Confirmed => Resolution::Applied(handoff),
        Phase::RolledBack => {
            let reason = handoff.error.clone().unwrap_or_else(|| "new daemon did not start".into());
            Resolution::Failed(handoff, reason)
        }
    })
}

/// The candidate completed startup: make it current.
pub fn confirm(dir: &Path, handoff: &Handoff) -> anyhow::Result<()> {
    set_current(dir, Some(&handoff.candidate))?;
    let mut confirmed = handoff.clone();
    confirmed.phase = Phase::Confirmed;
    write(dir, &confirmed)
}

/// After the deployment outcome is journaled: drop the handoff record and
/// every version but the current and previous ones (or, after a rollback,
/// the failed candidate).
pub fn finish(dir: &Path, handoff: &Handoff) -> anyhow::Result<()> {
    let keep: Vec<PathBuf> = [current(dir), handoff.previous.clone()]
        .into_iter()
        .flatten()
        .filter_map(|binary| binary.parent().map(Path::to_owned))
        .collect();
    if let Ok(entries) = std::fs::read_dir(dir.join("versions")) {
        for entry in entries.flatten() {
            if !keep.contains(&entry.path()) {
                let _ = std::fs::remove_dir_all(entry.path());
            }
        }
    }
    clear(dir)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, bytes)?;
    std::fs::File::open(&temporary)?.sync_all()?;
    std::fs::rename(&temporary, path)?;
    if let Some(parent) = path.parent() {
        sync_dir(parent)?;
    }
    Ok(())
}

fn sync_dir(dir: &Path) -> anyhow::Result<()> {
    std::fs::File::open(dir)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pending(attempts: u32) -> Handoff {
        Handoff {
            deployment: "0x9".into(),
            version: "2.0.0".into(),
            candidate: "/s/daemon/versions/2.0.0-ab/suifleet".into(),
            previous: Some("/s/daemon/versions/1.0.0-cd/suifleet".into()),
            attempts,
            phase: Phase::Pending,
            error: None,
        }
    }

    #[test]
    fn launcher_starts_base_confirmed_and_candidates_with_counted_attempts() {
        assert_eq!(decide(None, None, 3), (Start::Base, None));
        let current = Path::new("/s/daemon/versions/1.0.0-cd/suifleet");
        assert_eq!(decide(None, Some(current), 3), (Start::Confirmed(current.into()), None));
        let (start, update) = decide(Some(&pending(0)), Some(current), 3);
        assert_eq!(start, Start::Candidate(pending(0).candidate));
        assert_eq!(update.unwrap().attempts, 1, "an attempt is counted before it runs");
        let (start, update) = decide(Some(&pending(2)), Some(current), 3);
        assert_eq!((start, update.unwrap().attempts), (Start::Candidate(pending(0).candidate), 3));
    }

    #[test]
    fn exhausted_attempts_roll_back_to_the_previous_or_base_binary() {
        let (start, update) = decide(Some(&pending(3)), None, 3);
        assert_eq!(start, Start::Confirmed(pending(3).previous.unwrap()));
        let update = update.unwrap();
        assert_eq!(update.phase, Phase::RolledBack);
        assert!(update.error.unwrap().contains("did not complete startup in 3 attempt(s)"));
        let mut first = pending(1);
        first.previous = None;
        assert_eq!(decide(Some(&first), None, 1).0, Start::Base);
    }

    #[test]
    fn resolved_handoffs_do_not_change_what_starts() {
        let current = Path::new("/c/suifleet");
        for phase in [Phase::Confirmed, Phase::RolledBack] {
            let mut h = pending(1);
            h.phase = phase;
            assert_eq!(decide(Some(&h), Some(current), 3), (Start::Confirmed(current.into()), None));
        }
    }

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("suifleet-handoff-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn startup_resolution_by_phase_and_launcher_identity() {
        let d = dir("resolve");
        assert_eq!(resolve(&d, None).unwrap(), Resolution::None);
        write(&d, &pending(1)).unwrap();
        assert!(matches!(resolve(&d, Some("0x9")).unwrap(), Resolution::ConfirmAfterStartup(_)));
        assert!(matches!(resolve(&d, Some("0x8")).unwrap(), Resolution::Failed(_, r) if r.contains("not started by `suifleet --launch`")));
        assert!(matches!(resolve(&d, None).unwrap(), Resolution::Failed(..)));
        let mut h = pending(1);
        h.phase = Phase::RolledBack;
        h.error = Some("cannot be executed".into());
        write(&d, &h).unwrap();
        assert!(matches!(resolve(&d, None).unwrap(), Resolution::Failed(_, r) if r == "cannot be executed"));
    }

    #[test]
    fn confirm_makes_the_candidate_current_and_finish_keeps_two_versions() {
        let d = dir("confirm");
        let make = |name: &str| {
            let v = d.join("versions").join(name);
            std::fs::create_dir_all(&v).unwrap();
            std::fs::write(v.join("suifleet"), b"x").unwrap();
            v.join("suifleet")
        };
        let (old, prev, cand) = (make("0.9.0-aa"), make("1.0.0-cd"), make("2.0.0-ab"));
        let mut h = pending(1);
        h.candidate = cand.clone();
        h.previous = Some(prev.clone());
        write(&d, &h).unwrap();
        confirm(&d, &h).unwrap();
        assert_eq!(current(&d), Some(cand.clone()));
        assert!(matches!(resolve(&d, None).unwrap(), Resolution::Applied(_)));
        finish(&d, &h).unwrap();
        assert!(read(&d).unwrap().is_none());
        assert!(cand.exists() && prev.exists() && !old.exists());
    }

    #[test]
    fn a_rolled_back_candidate_is_removed_and_current_is_unchanged() {
        let d = dir("rollback");
        let v = d.join("versions/2.0.0-ab");
        std::fs::create_dir_all(&v).unwrap();
        let mut h = pending(3);
        h.candidate = v.join("suifleet");
        h.previous = None;
        h.phase = Phase::RolledBack;
        write(&d, &h).unwrap();
        finish(&d, &h).unwrap();
        assert!(!v.exists() && current(&d).is_none());
    }
}
