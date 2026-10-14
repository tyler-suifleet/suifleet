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

use crate::config::FirmwareHelperConfig;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    io::Read,
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
}

fn requests(spool: &Path) -> PathBuf {
    spool.join("requests")
}
fn results(spool: &Path) -> PathBuf {
    spool.join("results")
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
pub async fn submit(spool: &Path, artifact: &Path, timeout: Duration) -> anyhow::Result<()> {
    let (requests, results) = (requests(spool), results(spool));
    std::fs::create_dir_all(&requests)?;
    std::fs::create_dir_all(&results)?;
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let id = format!("{:032x}-{}", nonce, std::process::id());
    let staged = requests.join(format!("{id}.artifact"));
    let request = requests.join(format!("{id}.json"));
    let result = results.join(format!("{id}.json"));
    std::fs::copy(artifact, &staged)?;
    let sha256 = sha256_file(&staged)?;
    publish(
        &request,
        &serde_json::to_vec(&Request {
            id: id.clone(),
            sha256,
        })?,
    )?;
    let deadline = tokio::time::Instant::now() + timeout + HELPER_START_GRACE;
    loop {
        if let Ok(raw) = std::fs::read(&result) {
            let _ = std::fs::remove_file(&result);
            let outcome: Outcome = serde_json::from_slice(&raw)?;
            anyhow::ensure!(
                outcome.id == id,
                "firmware helper returned a mismatched result"
            );
            anyhow::ensure!(outcome.ok, "firmware helper: {}", outcome.message);
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            // Withdraw the request so a late helper cannot apply it unreported.
            let _ = std::fs::remove_file(&request);
            let _ = std::fs::remove_file(&staged);
            anyhow::bail!(
                "firmware helper did not respond within {}s; is suifleet-firmware.path enabled?",
                (timeout + HELPER_START_GRACE).as_secs()
            );
        }
        tokio::time::sleep(POLL).await;
    }
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
    let mut pending: Vec<String> = requests
        .names()?
        .into_iter()
        .filter(|name| name.ends_with(".json"))
        .collect();
    pending.sort();
    for name in &pending {
        let stem = name.trim_end_matches(".json").to_owned();
        let staged = format!("{stem}.artifact");
        let outcome = match process(config, &requests, &stem, name, &staged).await {
            Ok(()) => Outcome {
                id: stem.clone(),
                ok: true,
                message: "applied".into(),
            },
            Err(error) => Outcome {
                id: stem.clone(),
                ok: false,
                message: error.to_string(),
            },
        };
        // Always consume the request, even if invalid, so the path unit settles.
        let _ = requests.remove(name);
        let _ = requests.remove(&staged);
        if valid_id(&stem) {
            let temporary = format!("{stem}.tmp");
            let mut file = results.create_exclusive(&temporary)?;
            std::io::Write::write_all(&mut file, &serde_json::to_vec(&outcome)?)?;
            fchown(&file, uid, gid)?;
            file.sync_all()?;
            results.rename(&temporary, &format!("{stem}.json"))?;
        }
        tracing::info!(id = %stem, ok = outcome.ok, message = %outcome.message, "firmware request processed");
    }
    Ok(pending.len())
}

async fn process(
    config: &FirmwareHelperConfig,
    requests: &Dir,
    id: &str,
    request: &str,
    staged: &str,
) -> anyhow::Result<()> {
    anyhow::ensure!(valid_id(id), "invalid request id");
    let mut file = requests
        .open_regular(request)
        .map_err(|e| anyhow::anyhow!("request rejected: {e}"))?;
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
    fn names(&self) -> std::io::Result<Vec<String>> {
        // /proc/self/fd/N re-opens this exact directory, not a re-resolved path.
        Ok(std::fs::read_dir(format!("/proc/self/fd/{}", self.raw()))?
            .filter_map(|entry| entry.ok()?.file_name().into_string().ok())
            .collect())
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
            submit(&f.config.spool_dir, &f.artifact, Duration::from_secs(2)),
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
        let f = fixture("fail", "/bin/false");
        let (submitted, ()) = tokio::join!(
            submit(&f.config.spool_dir, &f.artifact, Duration::from_secs(2)),
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
            tokio::join!(submit(&spool, &f.artifact, Duration::from_secs(2)), tamper);
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
        let f = fixture("tmplink", "/bin/true");
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
        let f = fixture("dirlink", "/bin/true");
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

    #[tokio::test]
    async fn missing_helper_fails_and_withdraws_request() {
        let f = fixture("absent", copy_command());
        let started = std::time::Instant::now();
        // Zero updater timeout: only the start grace applies; shorten it for the test.
        let error = tokio::time::timeout(
            Duration::from_secs(40),
            submit(&f.config.spool_dir, &f.artifact, Duration::ZERO),
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
