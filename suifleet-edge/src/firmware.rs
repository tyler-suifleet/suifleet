//! Firmware-only privileged hand-off.
//!
//! The daemon runs unprivileged. When `apply.mode = "privileged-helper"` it
//! copies the verified artifact into `<spool>/requests/<id>.artifact`, then
//! atomically publishes `<id>.json` with the artifact's SHA-256. The root-owned
//! `suifleet-firmware.path` unit starts `suifleet --firmware-helper`, which:
//!
//! - accepts only well-formed IDs and regular, non-symlink artifact files;
//! - copies the artifact into its own root-only work directory and re-hashes
//!   that copy, so the daemon cannot swap bytes after submission;
//! - runs only the command from the root-owned helper config, bounded like
//!   every other child (timeout, process-group kill, limits);
//! - writes `<spool>/results/<id>.json` and always removes the request.
//!
//! A compromised daemon can therefore only submit an image to the configured
//! firmware updater, whose own image signature checks remain the final gate.
//!
//! Claiming and withdrawal race on one directory entry, so exactly one wins:
//!
//! - the helper locks `<id>.json` (flock) and renames it to `<id>.claimed`,
//!   holding the lock until the result is written;
//! - the daemon, once its wait expires, withdraws by unlinking `<id>.json`.
//!   Success means the updater never started, so Failed is accurate. Failure
//!   means the helper claimed it: the daemon keeps waiting while the lock is
//!   held, and if the helper died mid-update the outcome is [`Indeterminate`],
//!   never Failed.
//!
//! Request IDs derive from the deployment, so a result that arrives after the
//! daemon stopped waiting (or restarted) is still collected and reported. A
//! claim left by a helper that died (power loss, reboot) becomes an
//! "interrupted" result: the updater may or may not have finished.

use crate::config::FirmwareHelperConfig;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    io::Read,
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    time::Duration,
};

const POLL: Duration = Duration::from_millis(250);
/// Allowance for the path unit to start the helper on top of the updater timeout.
const HELPER_START_GRACE: Duration = Duration::from_secs(30);
const MAX_REQUEST_BYTES: u64 = 4096;

#[derive(Serialize, Deserialize, Debug, PartialEq)]
struct Request {
    id: String,
    sha256: String,
}

#[derive(Serialize, Deserialize, Debug)]
struct Outcome {
    id: String,
    ok: bool,
    message: String,
    /// The helper died while the updater ran; `ok` is meaningless.
    #[serde(default)]
    interrupted: bool,
}

/// The privileged update may have run, may still be running, or may have
/// been interrupted: it must be neither reported as Failed nor re-applied.
#[derive(Debug)]
pub struct Indeterminate(pub String);

impl std::fmt::Display for Indeterminate {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Indeterminate {}

fn requests(spool: &Path) -> PathBuf {
    spool.join("requests")
}
fn results(spool: &Path) -> PathBuf {
    spool.join("results")
}

/// Spool request ID for a deployment: stable across daemon restarts.
pub fn request_id(deployment_id: &str) -> String {
    hex::encode(Sha256::digest(deployment_id.as_bytes()))
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

fn sha256_file(path: &Path) -> anyhow::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// Write `contents` to `path` via a temporary file and rename, so readers never
/// observe a partial file.
fn publish(path: &Path, contents: &[u8]) -> anyhow::Result<()> {
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, contents)?;
    std::fs::rename(&temporary, path)?;
    Ok(())
}

/// Daemon side: submit a verified artifact and wait for the helper's outcome.
///
/// `timeout` bounds only the wait for the helper to claim the request. Once
/// claimed, the helper's own timeout bounds the updater and this waits for it.
pub async fn submit(
    spool: &Path,
    id: &str,
    artifact: &Path,
    timeout: Duration,
) -> anyhow::Result<()> {
    submit_within(spool, id, artifact, timeout + HELPER_START_GRACE).await
}

async fn submit_within(
    spool: &Path,
    id: &str,
    artifact: &Path,
    claim_within: Duration,
) -> anyhow::Result<()> {
    anyhow::ensure!(valid_id(id), "invalid firmware request id");
    let (requests, results) = (requests(spool), results(spool));
    std::fs::create_dir_all(&requests)?;
    std::fs::create_dir_all(&results)?;
    let staged = requests.join(format!("{id}.artifact"));
    let request = requests.join(format!("{id}.json"));
    let claimed = requests.join(format!("{id}.claimed"));
    std::fs::copy(artifact, &staged)?;
    let sha256 = sha256_file(&staged)?;
    publish(
        &request,
        &serde_json::to_vec(&Request {
            id: id.to_owned(),
            sha256,
        })?,
    ).map_err(|error| Indeterminate(format!("cannot confirm firmware request publication: {error}; completion unknown")))?;
    let deadline = tokio::time::Instant::now() + claim_within;
    loop {
        if let Some(outcome) = collect(spool, id).map_err(unavailable_result)? {
            return outcome;
        }
        if tokio::time::Instant::now() >= deadline {
            match std::fs::remove_file(&request) {
                Ok(()) => {
                    // Withdrawn before any claim: the updater never ran.
                    let _ = std::fs::remove_file(&staged);
                    anyhow::bail!(
                        "firmware helper did not respond within {}s; is suifleet-firmware.path enabled?",
                        claim_within.as_secs()
                    );
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(Indeterminate(format!("cannot confirm request withdrawal: {e}; completion unknown")).into()),
            }
            if !helper_active(&claimed) {
                // The helper writes its result before releasing the claim.
                return match collect(spool, id).map_err(unavailable_result)? {
                    Some(outcome) => outcome,
                    None => Err(Indeterminate(format!(
                        "firmware request {id} was claimed but the helper stopped without a result; \
                         the update may have been applied"
                    ))
                    .into()),
                };
            }
        }
        tokio::time::sleep(POLL).await;
    }
}

fn unavailable_result(error: anyhow::Error) -> Indeterminate {
    Indeterminate(format!("cannot read firmware command result: {error}; completion unknown"))
}

/// Read the helper's result for `id`, if it has written one. The result stays
/// in the spool until [`acknowledge`], so a crash before the outcome is
/// journaled does not lose it.
pub fn collect(spool: &Path, id: &str) -> anyhow::Result<Option<anyhow::Result<()>>> {
    let raw = match std::fs::read(results(spool).join(format!("{id}.json"))) {
        Ok(raw) => raw,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    };
    let outcome: Outcome = serde_json::from_slice(&raw)?;
    anyhow::ensure!(
        outcome.id == id,
        "firmware helper returned a mismatched result"
    );
    Ok(Some(if outcome.interrupted {
        Err(Indeterminate(format!("firmware helper: {}", outcome.message)).into())
    } else if outcome.ok {
        Ok(())
    } else {
        Err(anyhow::anyhow!("firmware helper: {}", outcome.message))
    }))
}

/// At daemon startup, for a deployment the previous run left without a status:
/// can its real outcome still be learned? A result or a claim means yes (the
/// collector will reconcile it). An unclaimed request is withdrawn first, so
/// it can never run unreported; then, like no request at all, the updater
/// never ran.
pub fn recover(spool: &Path, id: &str) -> anyhow::Result<bool> {
    anyhow::ensure!(valid_id(id), "invalid firmware request id");
    let requests = requests(spool);
    let known = || {
        results(spool).join(format!("{id}.json")).exists()
            || requests.join(format!("{id}.claimed")).exists()
    };
    if known() {
        return Ok(true);
    }
    match std::fs::remove_file(requests.join(format!("{id}.json"))) {
        Ok(()) => {}
        // Claimed between the checks: the helper owns it now.
        Err(e) if e.kind() == std::io::ErrorKind::NotFound && known() => return Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
    let _ = std::fs::remove_file(requests.join(format!("{id}.artifact")));
    Ok(false)
}

/// Drop a collected result once its outcome is durably journaled.
pub fn acknowledge(spool: &Path, id: &str) {
    let _ = std::fs::remove_file(results(spool).join(format!("{id}.json")));
}

/// Whether a helper still holds the claim on a request (it locks it for the
/// whole update). A missing claim file means the helper already finished.
fn helper_active(claimed: &Path) -> bool {
    match std::fs::File::open(claimed) {
        Ok(file) => !try_lock(&file, libc::LOCK_SH),
        Err(_) => false,
    }
}

/// Non-blocking flock; false if another open file description holds a
/// conflicting lock. The lock is released when `file` is closed.
fn try_lock(file: &std::fs::File, mode: libc::c_int) -> bool {
    unsafe { libc::flock(file.as_raw_fd(), mode | libc::LOCK_NB) == 0 }
}

/// Root side (`suifleet --firmware-helper CONFIG`): process every pending request once.
///
/// The spool is writable by the unprivileged daemon, so the helper never
/// resolves paths inside it: every open/unlink/rename goes through directory
/// descriptors opened with O_NOFOLLOW, and new files are created with O_EXCL.
/// A planted symlink therefore cannot redirect root's reads, writes or deletes.
pub async fn run_helper(config: &FirmwareHelperConfig) -> anyhow::Result<usize> {
    let spool = match Dir::open(&config.spool_dir) {
        Ok(dir) => dir,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(anyhow::anyhow!("spool {}: {e}", config.spool_dir.display())),
    };
    let requests = match spool.child("requests") {
        Ok(dir) => dir,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(anyhow::anyhow!("spool requests: {e}")),
    };
    let results = spool
        .child("results")
        .map_err(|e| anyhow::anyhow!("spool results: {e}"))?;
    // Results are handed back to whoever owns the spool (the daemon user).
    let (uid, gid) = spool.owner()?;
    create_private_dir(&config.work_dir)?;
    let names = requests.names()?;
    let respond = |stem: &str, outcome: &Outcome| -> anyhow::Result<()> {
        let temporary = format!("{stem}.tmp");
        let mut file = results.create_exclusive(&temporary)?;
        std::io::Write::write_all(&mut file, &serde_json::to_vec(outcome)?)?;
        fchown(&file, uid, gid)?;
        file.sync_all()?;
        results.rename(&temporary, &format!("{stem}.json"))?;
        tracing::info!(id = %stem, ok = outcome.ok, interrupted = outcome.interrupted,
            message = %outcome.message, "firmware request processed");
        Ok(())
    };
    let mut processed = 0;
    // A claim nobody holds was left by a helper that died mid-update.
    for name in names.iter().filter(|name| name.ends_with(".claimed")) {
        let stem = name.trim_end_matches(".claimed");
        let abandoned = match requests.open_regular(name) {
            Ok(file) => try_lock(&file, libc::LOCK_EX),
            Err(_) => true,
        };
        if !abandoned {
            continue; // Another helper instance is still applying it.
        }
        if valid_id(stem) {
            respond(
                stem,
                &Outcome {
                    id: stem.to_owned(),
                    ok: false,
                    message: "interrupted while the updater was running; outcome unknown".into(),
                    interrupted: true,
                },
            )?;
        }
        let _ = requests.remove(name);
        let _ = requests.remove(&format!("{stem}.artifact"));
        processed += 1;
    }
    let mut pending: Vec<&String> = names.iter().filter(|name| name.ends_with(".json")).collect();
    pending.sort();
    for name in pending {
        let stem = name.trim_end_matches(".json").to_owned();
        let staged = format!("{stem}.artifact");
        let claim = format!("{stem}.claimed");
        // Lock, then claim by rename: losing the rename means the daemon withdrew it.
        let request = match requests.open_regular(name) {
            Ok(file) if !try_lock(&file, libc::LOCK_EX) => continue,
            Ok(file) => match requests.rename(name, &claim) {
                Ok(()) => Ok(file),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(e) => return Err(anyhow::anyhow!("claim {name}: {e}")),
            },
            Err(e) => Err(anyhow::anyhow!("request rejected: {e}")),
        };
        let (result, _claim_lock) = match request {
            Ok(mut file) => (
                process(config, &requests, &stem, &mut file, &staged).await,
                Some(file),
            ),
            Err(error) => (Err(error), None),
        };
        let outcome = Outcome {
            id: stem.clone(),
            ok: result.is_ok(),
            message: match &result {
                Ok(()) => "configured command completed (exit 0); platform boot confirmation not inferred".into(),
                Err(error) => error.to_string(),
            },
            interrupted: result.as_ref().err().is_some_and(|e| e.downcast_ref::<Indeterminate>().is_some()),
        };
        // Answer while `_claim_lock` still holds the claim, then always consume
        // the request, even if invalid, so the path unit settles.
        if valid_id(&stem) {
            respond(&stem, &outcome)?;
        }
        let _ = requests.remove(name);
        let _ = requests.remove(&claim);
        let _ = requests.remove(&staged);
        processed += 1;
    }
    Ok(processed)
}

async fn process(
    config: &FirmwareHelperConfig,
    requests: &Dir,
    id: &str,
    file: &mut std::fs::File,
    staged: &str,
) -> anyhow::Result<()> {
    anyhow::ensure!(valid_id(id), "invalid request id");
    anyhow::ensure!(
        file.metadata()?.len() <= MAX_REQUEST_BYTES,
        "request is too large"
    );
    let mut raw = Vec::new();
    file.read_to_end(&mut raw)?;
    let parsed: Request = serde_json::from_slice(&raw)?;
    anyhow::ensure!(parsed.id == id, "request id does not match its file name");
    anyhow::ensure!(
        parsed.sha256.len() == 64 && parsed.sha256.bytes().all(|b| b.is_ascii_hexdigit()),
        "request has no valid SHA-256"
    );
    let copy = config.work_dir.join(format!("{id}.artifact"));
    let result = async {
        let mut input = requests
            .open_regular(staged)
            .map_err(|e| anyhow::anyhow!("staged artifact rejected: {e}"))?;
        copy_into_private(&mut input, &copy)?;
        let actual = sha256_file(&copy)?;
        anyhow::ensure!(
            actual.eq_ignore_ascii_case(&parsed.sha256),
            "artifact SHA-256 mismatch: expected {} got {actual}",
            parsed.sha256
        );
        let command = crate::apply::command(&config.command, &copy)?;
        crate::apply::run_bounded(
            command,
            &config.work_dir,
            Duration::from_secs(config.timeout_seconds),
            config.max_memory_bytes,
        )
        .await
    }
    .await;
    let _ = std::fs::remove_file(&copy);
    result
}

/// Copy into the helper's root-only work directory, replacing any old copy.
fn copy_into_private(input: &mut std::fs::File, destination: &Path) -> anyhow::Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    let _ = std::fs::remove_file(destination);
    let mut output = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(destination)?;
    std::io::copy(input, &mut output)?;
    Ok(())
}

fn create_private_dir(path: &Path) -> anyhow::Result<()> {
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    anyhow::ensure!(
        !std::fs::symlink_metadata(path)?.file_type().is_symlink(),
        "work directory must not be a symlink"
    );
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    Ok(())
}

fn fchown(file: &std::fs::File, uid: u32, gid: u32) -> std::io::Result<()> {
    use std::os::fd::AsRawFd;
    if unsafe { libc::fchown(file.as_raw_fd(), uid, gid) } != 0 {
        let error = std::io::Error::last_os_error();
        // Unprivileged test runs already own the spool; anything else is fatal.
        if error.raw_os_error() != Some(libc::EPERM) || unsafe { libc::geteuid() } == 0 {
            return Err(error);
        }
    }
    Ok(())
}

/// A directory opened without following symlinks; children are reached with
/// *at() calls relative to it.
struct Dir(std::os::fd::OwnedFd);

impl Dir {
    const FLAGS: libc::c_int =
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;

    fn open(path: &Path) -> std::io::Result<Self> {
        let path = std::ffi::CString::new(path.as_os_str().as_encoded_bytes())?;
        Self::wrap(unsafe { libc::open(path.as_ptr(), Self::FLAGS) })
    }
    fn child(&self, name: &str) -> std::io::Result<Self> {
        let name = Self::name(name)?;
        Self::wrap(unsafe { libc::openat(self.raw(), name.as_ptr(), Self::FLAGS) })
    }
    fn wrap(fd: libc::c_int) -> std::io::Result<Self> {
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Self(unsafe { std::os::fd::FromRawFd::from_raw_fd(fd) }))
    }
    fn raw(&self) -> libc::c_int {
        std::os::fd::AsRawFd::as_raw_fd(&self.0)
    }
    fn name(name: &str) -> std::io::Result<std::ffi::CString> {
        if name.is_empty() || name.contains('/') || name == "." || name == ".." {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "invalid entry name",
            ));
        }
        Ok(std::ffi::CString::new(name)?)
    }
    /// Open an existing regular file (never a symlink, FIFO or device).
    fn open_regular(&self, name: &str) -> std::io::Result<std::fs::File> {
        let name = Self::name(name)?;
        let fd = unsafe {
            libc::openat(
                self.raw(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let file: std::fs::File = unsafe { std::os::fd::FromRawFd::from_raw_fd(fd) };
        if !file.metadata()?.is_file() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "not a regular file",
            ));
        }
        Ok(file)
    }
    /// Create a new file, removing any pre-existing entry (including a planted symlink).
    fn create_exclusive(&self, name: &str) -> std::io::Result<std::fs::File> {
        let _ = self.remove(name);
        let name = Self::name(name)?;
        let fd = unsafe {
            libc::openat(
                self.raw(),
                name.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600 as libc::c_uint,
            )
        };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(unsafe { std::os::fd::FromRawFd::from_raw_fd(fd) })
    }
    fn remove(&self, name: &str) -> std::io::Result<()> {
        let name = Self::name(name)?;
        if unsafe { libc::unlinkat(self.raw(), name.as_ptr(), 0) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
    fn rename(&self, from: &str, to: &str) -> std::io::Result<()> {
        let (from, to) = (Self::name(from)?, Self::name(to)?);
        if unsafe { libc::renameat(self.raw(), from.as_ptr(), self.raw(), to.as_ptr()) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
    #[cfg(target_os = "linux")]
    fn names(&self) -> std::io::Result<Vec<String>> {
        // /proc/self/fd/N re-opens this exact directory, not a re-resolved path.
        Ok(std::fs::read_dir(format!("/proc/self/fd/{}", self.raw()))?
            .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
            .collect())
    }
    #[cfg(not(target_os = "linux"))]
    fn names(&self) -> std::io::Result<Vec<String>> {
        // A duplicate of this exact directory's descriptor, not a re-resolved path.
        let fd = unsafe { libc::dup(self.raw()) };
        if fd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let dir = unsafe { libc::fdopendir(fd) };
        if dir.is_null() {
            let error = std::io::Error::last_os_error();
            unsafe { libc::close(fd) };
            return Err(error);
        }
        // The duplicate shares the directory offset: read from the start.
        unsafe { libc::rewinddir(dir) };
        let mut names = Vec::new();
        loop {
            let entry = unsafe { libc::readdir(dir) };
            if entry.is_null() {
                break;
            }
            let name = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) };
            if let Ok(name) = name.to_str() {
                if name != "." && name != ".." {
                    names.push(name.to_owned());
                }
            }
        }
        unsafe { libc::closedir(dir) };
        Ok(names)
    }
    fn owner(&self) -> std::io::Result<(u32, u32)> {
        let mut stat: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstat(self.raw(), &mut stat) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok((stat.st_uid, stat.st_gid))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fixture {
        root: PathBuf,
        config: FirmwareHelperConfig,
        artifact: PathBuf,
        marker: PathBuf,
    }

    fn fixture(name: &str, command: &str) -> Fixture {
        let root = std::env::temp_dir().join(format!("suifleet-fw-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("daemon")).unwrap();
        let artifact = root.join("daemon/artifact");
        std::fs::write(&artifact, b"firmware image").unwrap();
        let marker = root.join("applied");
        let config = FirmwareHelperConfig {
            command: command.replace("{marker}", marker.to_str().unwrap()),
            timeout_seconds: 2,
            spool_dir: root.join("spool"),
            work_dir: root.join("work"),
            max_memory_bytes: None,
        };
        Fixture {
            root,
            config,
            artifact,
            marker,
        }
    }

    const ID: &str = "0123abcd";

    /// Run the helper until it has processed one request (as the path unit would).
    async fn helper_once(config: &FirmwareHelperConfig) {
        for _ in 0..200 {
            if run_helper(config).await.unwrap() > 0 {
                return;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        panic!("no request arrived");
    }

    fn copy_command() -> &'static str {
        // Records the verified copy's bytes; the artifact path is appended as $0.
        r#"/bin/sh -c 'cat "$0" > {marker}'"#
    }

    #[tokio::test]
    async fn verified_request_runs_configured_command() {
        let f = fixture("ok", copy_command());
        let (submitted, ()) = tokio::join!(
            submit(&f.config.spool_dir, ID, &f.artifact, Duration::from_secs(2)),
            helper_once(&f.config)
        );
        submitted.unwrap();
        assert_eq!(std::fs::read(&f.marker).unwrap(), b"firmware image");
        assert_eq!(
            std::fs::read_dir(requests(&f.config.spool_dir))
                .unwrap()
                .count(),
            0
        );
        assert_eq!(std::fs::read_dir(&f.config.work_dir).unwrap().count(), 0);
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn updater_failure_is_reported_not_applied() {
        let f = fixture("fail", "/usr/bin/false");
        let (submitted, ()) = tokio::join!(
            submit(&f.config.spool_dir, ID, &f.artifact, Duration::from_secs(2)),
            helper_once(&f.config)
        );
        let error = submitted.unwrap_err().to_string();
        assert!(
            error.contains("firmware helper") && error.contains("exit code 1"),
            "{error}"
        );
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn swapped_artifact_is_rejected_before_running() {
        let f = fixture("swap", copy_command());
        let spool = f.config.spool_dir.clone();
        let tamper = async {
            // Replace the staged bytes after the request is published.
            let requests = requests(&spool);
            loop {
                let staged: Vec<_> = std::fs::read_dir(&requests)
                    .map(|d| d.filter_map(|e| e.ok()).map(|e| e.path()).collect())
                    .unwrap_or_default();
                if staged
                    .iter()
                    .any(|p| p.extension().is_some_and(|x| x == "json"))
                {
                    for path in staged
                        .iter()
                        .filter(|p| p.extension().is_some_and(|x| x == "artifact"))
                    {
                        std::fs::write(path, b"malicious image").unwrap();
                    }
                    break;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            helper_once(&f.config).await;
        };
        let (submitted, ()) =
            tokio::join!(submit(&spool, ID, &f.artifact, Duration::from_secs(2)), tamper);
        let error = submitted.unwrap_err().to_string();
        assert!(error.contains("SHA-256 mismatch"), "{error}");
        assert!(!f.marker.exists(), "updater ran on a tampered artifact");
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn symlinked_artifact_and_bad_ids_are_rejected() {
        let f = fixture("symlink", copy_command());
        let requests = requests(&f.config.spool_dir);
        std::fs::create_dir_all(&requests).unwrap();
        std::fs::create_dir_all(results(&f.config.spool_dir)).unwrap();
        std::os::unix::fs::symlink("/etc/passwd", requests.join("abc.artifact")).unwrap();
        let sha = sha256_file(Path::new("/etc/passwd")).unwrap();
        std::fs::write(
            requests.join("abc.json"),
            serde_json::to_vec(&Request {
                id: "abc".into(),
                sha256: sha,
            })
            .unwrap(),
        )
        .unwrap();
        std::fs::write(requests.join("..%2fevil.json"), b"{}").unwrap();
        assert_eq!(run_helper(&f.config).await.unwrap(), 2);
        let outcome: Outcome = serde_json::from_slice(
            &std::fs::read(results(&f.config.spool_dir).join("abc.json")).unwrap(),
        )
        .unwrap();
        assert!(
            !outcome.ok && outcome.message.contains("staged artifact rejected"),
            "{outcome:?}"
        );
        assert!(!f.marker.exists());
        assert_eq!(
            std::fs::read_dir(&requests).unwrap().count(),
            0,
            "requests must be consumed"
        );
        let _ = std::fs::remove_dir_all(f.root);
    }

    /// Write a well-formed request whose artifact matches, ready for the helper.
    fn stage_request(spool: &Path, id: &str) {
        let requests = requests(spool);
        std::fs::create_dir_all(&requests).unwrap();
        std::fs::create_dir_all(results(spool)).unwrap();
        std::fs::write(requests.join(format!("{id}.artifact")), b"image").unwrap();
        let sha256 = hex::encode(Sha256::digest(b"image"));
        std::fs::write(
            requests.join(format!("{id}.json")),
            serde_json::to_vec(&Request {
                id: id.into(),
                sha256,
            })
            .unwrap(),
        )
        .unwrap();
    }

    #[tokio::test]
    async fn planted_result_symlink_cannot_redirect_root_writes() {
        let f = fixture("tmplink", "/usr/bin/true");
        stage_request(&f.config.spool_dir, "abc");
        let victim = f.root.join("victim");
        std::fs::write(&victim, b"untouched").unwrap();
        let results = results(&f.config.spool_dir);
        std::os::unix::fs::symlink(&victim, results.join("abc.tmp")).unwrap();
        assert_eq!(run_helper(&f.config).await.unwrap(), 1);
        assert_eq!(std::fs::read(&victim).unwrap(), b"untouched");
        let outcome: Outcome =
            serde_json::from_slice(&std::fs::read(results.join("abc.json")).unwrap()).unwrap();
        assert!(outcome.ok, "{outcome:?}");
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn symlinked_spool_directories_are_refused() {
        let f = fixture("dirlink", "/usr/bin/true");
        // A daemon swaps requests/ for a link to a directory it wants root to edit.
        let target = f.root.join("target");
        stage_request(&target, "abc");
        std::fs::create_dir_all(&f.config.spool_dir).unwrap();
        std::os::unix::fs::symlink(requests(&target), requests(&f.config.spool_dir)).unwrap();
        std::fs::create_dir_all(results(&f.config.spool_dir)).unwrap();
        let error = run_helper(&f.config).await.unwrap_err().to_string();
        assert!(error.contains("spool requests"), "{error}");
        assert!(
            requests(&target).join("abc.json").exists(),
            "root followed the link"
        );
        let _ = std::fs::remove_dir_all(f.root);
    }

    fn entries(dir: &Path) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(dir)
            .map(|d| {
                d.filter_map(|e| e.ok()?.file_name().into_string().ok())
                    .collect()
            })
            .unwrap_or_default();
        names.sort();
        names
    }

    #[tokio::test]
    async fn claimed_update_outliving_the_wait_is_awaited_not_failed() {
        // The review reproduction: the daemon's wait expires while the helper,
        // with its own longer timeout, is still running the updater.
        let f = fixture("inflight", r#"/bin/sh -c 'sleep 1; cat "$0" > {marker}'"#);
        let started = std::time::Instant::now();
        let (submitted, ()) = tokio::join!(
            submit_within(&f.config.spool_dir, ID, &f.artifact, Duration::from_millis(200)),
            helper_once(&f.config)
        );
        submitted.unwrap();
        assert!(started.elapsed() >= Duration::from_secs(1));
        assert_eq!(std::fs::read(&f.marker).unwrap(), b"firmware image");
        assert!(entries(&requests(&f.config.spool_dir)).is_empty());
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn unclaimed_request_is_withdrawn_and_never_applied() {
        // Queue delay: the helper only gets to the request after the wait.
        let f = fixture("late", copy_command());
        let error = submit_within(&f.config.spool_dir, ID, &f.artifact, Duration::from_millis(200))
            .await
            .unwrap_err();
        assert!(error.downcast_ref::<Indeterminate>().is_none(), "{error:#}");
        assert!(error.to_string().contains("did not respond"), "{error}");
        assert_eq!(run_helper(&f.config).await.unwrap(), 0);
        assert!(!f.marker.exists(), "a withdrawn request must never run");
        assert!(entries(&requests(&f.config.spool_dir)).is_empty());
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn helper_dying_mid_update_is_indeterminate_then_interrupted() {
        let f = fixture("died", copy_command());
        let spool = f.config.spool_dir.clone();
        // A helper that claims the request and dies (power loss): the claim
        // exists but nobody holds its lock and no result is written.
        let die_after_claim = async {
            let request = requests(&spool).join(format!("{ID}.json"));
            while !request.exists() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            std::fs::rename(&request, requests(&spool).join(format!("{ID}.claimed"))).unwrap();
        };
        let (submitted, ()) = tokio::join!(
            submit_within(&spool, ID, &f.artifact, Duration::from_millis(200)),
            die_after_claim
        );
        let error = submitted.unwrap_err();
        assert!(error.downcast_ref::<Indeterminate>().is_some(), "{error:#}");
        assert!(collect(&spool, ID).unwrap().is_none());
        // The next helper run turns the abandoned claim into an interrupted
        // result without re-running the updater.
        assert_eq!(run_helper(&f.config).await.unwrap(), 1);
        assert!(!f.marker.exists());
        assert!(entries(&requests(&spool)).is_empty());
        let late = collect(&spool, ID).unwrap().unwrap().unwrap_err();
        assert!(late.downcast_ref::<Indeterminate>().is_some(), "{late:#}");
        acknowledge(&spool, ID);
        assert!(collect(&spool, ID).unwrap().is_none());
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn unavailable_helper_result_stays_unknown_and_can_be_reconciled() {
        let f = fixture("unavailable-result", "/usr/bin/true");
        let spool = &f.config.spool_dir;
        let damage_result = async {
            let request = requests(spool).join(format!("{ID}.json"));
            while !request.exists() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
            std::fs::rename(request, requests(spool).join(format!("{ID}.claimed"))).unwrap();
            std::fs::write(results(spool).join(format!("{ID}.json")), "unreadable result").unwrap();
        };
        let (result, ()) = tokio::join!(
            submit_within(spool, ID, &f.artifact, Duration::from_secs(1)),
            damage_result
        );
        let error = result.unwrap_err();
        assert!(error.downcast_ref::<Indeterminate>().is_some(), "{error:#}");
        assert!(results(spool).join(format!("{ID}.json")).exists());
        // Platform/operator repair supplies a real result, without repeating
        // the installer. It remains collectable until durably acknowledged.
        std::fs::write(results(spool).join(format!("{ID}.json")), serde_json::to_vec(&Outcome {
            id: ID.into(), ok: true, message: "command completed".into(), interrupted: false,
        }).unwrap()).unwrap();
        assert!(collect(spool, ID).unwrap().unwrap().is_ok());
        assert!(!f.marker.exists(), "an unknown command must not be repeated");
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn helper_timeout_persists_unknown_instead_of_supplied_failure() {
        let mut f = fixture("timeout-unknown", "/bin/sh -c 'exec sleep 300'");
        f.config.timeout_seconds = 1;
        let (result, ()) = tokio::join!(
            submit(&f.config.spool_dir, ID, &f.artifact, Duration::from_secs(3)),
            helper_once(&f.config)
        );
        let error = result.unwrap_err();
        assert!(error.downcast_ref::<Indeterminate>().is_some(), "{error:#}");
        let raw = std::fs::read(results(&f.config.spool_dir).join(format!("{ID}.json"))).unwrap();
        let outcome: Outcome = serde_json::from_slice(&raw).unwrap();
        assert!(outcome.interrupted && !outcome.ok);
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn live_claim_is_not_swept_as_abandoned() {
        let f = fixture("live", "/usr/bin/true");
        stage_request(&f.config.spool_dir, ID);
        let requests = requests(&f.config.spool_dir);
        let claimed = requests.join(format!("{ID}.claimed"));
        std::fs::rename(requests.join(format!("{ID}.json")), &claimed).unwrap();
        let holder = std::fs::File::open(&claimed).unwrap();
        assert!(try_lock(&holder, libc::LOCK_EX));
        assert!(helper_active(&claimed));
        assert_eq!(run_helper(&f.config).await.unwrap(), 0);
        assert!(claimed.exists(), "an active claim must be left alone");
        assert!(collect(&f.config.spool_dir, ID).unwrap().is_none());
        drop(holder);
        // Children forked by concurrent tests can share the lock until they exec.
        let mut released = false;
        for _ in 0..100 {
            if !helper_active(&claimed) {
                released = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(released, "the claim lock must be released with its holder");
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn late_result_is_collected_until_acknowledged() {
        let f = fixture("collect", "/usr/bin/true");
        stage_request(&f.config.spool_dir, ID);
        assert_eq!(run_helper(&f.config).await.unwrap(), 1);
        assert!(collect(&f.config.spool_dir, ID).unwrap().unwrap().is_ok());
        assert!(collect(&f.config.spool_dir, ID).unwrap().is_some(), "kept until acknowledged");
        acknowledge(&f.config.spool_dir, ID);
        assert!(collect(&f.config.spool_dir, ID).unwrap().is_none());
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[tokio::test]
    async fn recover_keeps_learnable_outcomes_and_withdraws_unclaimed_requests() {
        let f = fixture("recover", "/usr/bin/true");
        let spool = &f.config.spool_dir;
        // A written result and a claim can still be reconciled.
        stage_request(spool, "done");
        assert_eq!(run_helper(&f.config).await.unwrap(), 1);
        assert!(recover(spool, "done").unwrap());
        stage_request(spool, "claimed");
        let requests = requests(spool);
        std::fs::rename(requests.join("claimed.json"), requests.join("claimed.claimed")).unwrap();
        assert!(recover(spool, "claimed").unwrap());
        // An unclaimed request is withdrawn: it never runs, and Failed is accurate.
        stage_request(spool, "queued");
        assert!(!recover(spool, "queued").unwrap());
        assert!(!requests.join("queued.json").exists() && !requests.join("queued.artifact").exists());
        assert_eq!(run_helper(&f.config).await.unwrap(), 1, "only the abandoned claim is left");
        assert!(collect(spool, "queued").unwrap().is_none());
        // Never submitted.
        assert!(!recover(spool, "absent").unwrap());
        assert!(recover(spool, "../x").is_err());
        let _ = std::fs::remove_dir_all(f.root);
    }

    #[test]
    fn request_ids_are_stable_and_valid() {
        let id = request_id("0xabc");
        assert_eq!(id, request_id("0xabc"));
        assert_ne!(id, request_id("0xabd"));
        assert!(valid_id(&id));
    }

    #[tokio::test]
    async fn missing_helper_fails_and_withdraws_request() {
        let f = fixture("absent", copy_command());
        let started = std::time::Instant::now();
        // Zero updater timeout: only the start grace applies; shorten it for the test.
        let error = tokio::time::timeout(
            Duration::from_secs(40),
            submit(&f.config.spool_dir, ID, &f.artifact, Duration::ZERO),
        )
        .await
        .unwrap()
        .unwrap_err()
        .to_string();
        assert!(error.contains("did not respond"), "{error}");
        assert!(started.elapsed() >= HELPER_START_GRACE);
        assert_eq!(
            std::fs::read_dir(requests(&f.config.spool_dir))
                .unwrap()
                .count(),
            0
        );
        let _ = std::fs::remove_dir_all(f.root);
    }
}
