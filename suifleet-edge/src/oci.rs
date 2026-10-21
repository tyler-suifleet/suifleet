//! Docker Engine OCI adapter. No shell, inherited credentials, arbitrary engine
//! flags, host mounts or engine-managed restarts cross this boundary.
use anyhow::{ensure, Context};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

pub const RUN: &str = "suifleet:oci";
const LABEL: &str = "io.suifleet.supervisor";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Descriptor {
    pub version: u8,
    pub image: String,
    pub platform: String,
    pub user: String,
    pub memory_bytes: u64,
    pub cpus: u32,
    pub pids_limit: u32,
    pub network: String,
    pub tmpfs_bytes: u64,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub readiness: Vec<String>,
    /// Device-local Docker auth profile, never credential contents or a path.
    pub registry_auth: Option<String>,
}
impl Descriptor {
    pub fn load(artifact: &Path) -> anyhow::Result<Self> {
        ensure!(
            std::fs::metadata(artifact)?.len() <= 64 * 1024,
            "OCI descriptor exceeds 64 KiB"
        );
        let bytes = std::fs::read(artifact)?;
        ensure!(bytes.len() <= 64 * 1024, "OCI descriptor exceeds 64 KiB");
        let value: Self = serde_json::from_slice(&bytes)
            .map_err(|_| anyhow::anyhow!("invalid OCI descriptor"))?;
        value.validate()?;
        Ok(value)
    }
    fn validate(&self) -> anyhow::Result<()> {
        ensure!(
            cfg!(target_os = "linux") && host_platform() != "unsupported" && self.version == 1,
            "unsupported OCI platform/schema"
        );
        let (repository, digest) = self
            .image
            .split_once("@sha256:")
            .context("OCI image needs immutable sha256 digest")?;
        ensure!(
            repository.contains('/')
                && !repository.starts_with('-')
                && repository
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"./:_-".contains(&b))
                && digest.len() == 64
                && digest
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
            "invalid OCI image reference"
        );
        let platform = host_platform();
        ensure!(
            self.platform == platform,
            "OCI platform differs from this host"
        );
        ensure!(
            !self.user.is_empty()
                && self.user.split(':').count() <= 2
                && self
                    .user
                    .split(':')
                    .all(|s| s.parse::<u32>().is_ok_and(|id| id > 0)),
            "OCI user must be numeric non-root UID[:GID]"
        );
        ensure!(
            (6 * 1024 * 1024..=64 * 1024 * 1024 * 1024).contains(&self.memory_bytes)
                && (1..=64).contains(&self.cpus)
                && (1..=4096).contains(&self.pids_limit)
                && self.tmpfs_bytes <= self.memory_bytes,
            "invalid OCI resource limits"
        );
        ensure!(
            matches!(self.network.as_str(), "none" | "bridge"),
            "OCI network must be none or bridge"
        );
        ensure!(
            self.command.len() <= 128
                && self.readiness.len() <= 128
                && self
                    .command
                    .iter()
                    .chain(&self.readiness)
                    .all(|s| s.len() <= 4096 && !s.contains('\0')),
            "invalid OCI command"
        );
        if let Some(profile) = &self.registry_auth {
            ensure!(
                !profile.is_empty()
                    && profile.len() <= 64
                    && profile
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'),
                "invalid OCI auth profile"
            );
        }
        Ok(())
    }
    fn config(&self) -> anyhow::Result<PathBuf> {
        self.config_at(Path::new("/etc/suifleet/oci"))
    }
    fn config_at(&self, base: &Path) -> anyhow::Result<PathBuf> {
        let Some(profile) = &self.registry_auth else {
            return Ok(base.join("public"));
        };
        let directory = base.join(profile);
        for path in [base, directory.as_path()] {
            let m = std::fs::symlink_metadata(path)?;
            ensure!(
                m.is_dir()
                    && !m.file_type().is_symlink()
                    && (m.uid() == 0 || m.uid() == unsafe { libc::geteuid() })
                    && m.mode() & 0o022 == 0,
                "insecure OCI auth directory"
            );
        }
        let file = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(directory.join("config.json"))?;
        let m = file.metadata()?;
        ensure!(
            m.is_file()
                && (m.uid() == 0 || m.uid() == unsafe { libc::geteuid() })
                && m.mode() & 0o077 == 0
                && m.len() <= 64 * 1024,
            "insecure OCI auth file"
        );
        validate_auth(file)?;
        Ok(directory)
    }
}

fn validate_auth(file: std::fs::File) -> anyhow::Result<()> {
    let value: serde_json::Value = serde_json::from_reader(file)?;
    ensure!(
        value
            .as_object()
            .is_some_and(|v| v.keys().all(|k| k == "auths")),
        "OCI auth profile supports inline auths only; credential plugins are disabled"
    );
    Ok(())
}

pub fn host_platform() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => "linux/amd64",
        "aarch64" => "linux/arm64",
        "arm" => "linux/arm/v7",
        _ => "unsupported",
    }
}

fn scope(root: &Path) -> String {
    hex::encode(Sha256::digest(root.as_os_str().as_encoded_bytes()))
}
fn name(workspace: &Path) -> String {
    format!("suifleet-{}", scope(workspace))
}
fn command(config: &Path) -> tokio::process::Command {
    let mut c = tokio::process::Command::new("/usr/bin/docker");
    c.args(["--host", "unix:///var/run/docker.sock", "--config"])
        .arg(config);
    crate::apply::harden(&mut c, Path::new("/"), None);
    c
}
async fn output(config: &Path, args: &[String], seconds: u64) -> anyhow::Result<Vec<u8>> {
    if config == public() && config.join("config.json").exists() {
        let value: serde_json::Value =
            serde_json::from_slice(&std::fs::read(config.join("config.json"))?)?;
        ensure!(
            value.as_object().is_some_and(|v| v.is_empty()),
            "anonymous OCI config must be empty"
        );
    }
    let mut c = command(config);
    c.args(args).stdout(Stdio::piped()).stderr(Stdio::null());
    use tokio::io::AsyncReadExt;
    let mut child = crate::supervisor::spawn_tracked(&mut c)?;
    let mut stdout = child
        .take_stdout()
        .context("missing OCI engine output")?
        .take(1024 * 1024 + 1);
    let result = tokio::time::timeout(Duration::from_secs(seconds), async {
        let mut bytes = Vec::new();
        stdout.read_to_end(&mut bytes).await?;
        ensure!(
            bytes.len() <= 1024 * 1024,
            "OCI engine response exceeds limit"
        );
        ensure!(
            child.wait().await?.success(),
            "OCI engine operation failed (credentials and engine diagnostics withheld)"
        );
        Ok(bytes)
    })
    .await
    .context("OCI engine timed out")?;
    // Engine clients do not spawn children. On cancellation kill_on_drop
    // closes the client; engine-side reconciliation is always required.
    result
}

fn args(values: &[&str]) -> Vec<String> {
    values.iter().map(|s| (*s).into()).collect()
}
fn canonical(value: &str) -> String {
    let value = value.strip_prefix("docker.io/").unwrap_or(value);
    let value = value.strip_prefix("library/").unwrap_or(value);
    value.to_owned()
}
fn public() -> &'static Path {
    Path::new("/etc/suifleet/oci/public")
}

/// Pull only during deployment. Bootstrap, crash restart and rollback require
/// the retained exact image; they never contact a registry or resolve a tag.
pub async fn prepare(artifact: &Path) -> anyhow::Result<()> {
    let d = Descriptor::load(artifact)?;
    if verify(&d).await.is_err() {
        let config = d.config()?;
        output(
            &config,
            &args(&["image", "pull", "--platform", &d.platform, &d.image]),
            120,
        )
        .await?;
        verify(&d).await?;
    }
    Ok(())
}
async fn verify(d: &Descriptor) -> anyhow::Result<()> {
    let bytes = output(public(), &args(&["image", "inspect", &d.image]), 20).await?;
    let values: Vec<serde_json::Value> = serde_json::from_slice(&bytes)?;
    let v = values.first().context("missing OCI image")?;
    let platform = format!(
        "{}/{}{}",
        v["Os"].as_str().unwrap_or(""),
        v["Architecture"].as_str().unwrap_or(""),
        v["Variant"]
            .as_str()
            .filter(|s| !s.is_empty())
            .map(|s| format!("/{s}"))
            .unwrap_or_default()
    );
    // arm64/v8 is the canonical baseline for linux/arm64.
    ensure!(
        platform == d.platform || (d.platform == "linux/arm64" && platform == "linux/arm64/v8"),
        "cached OCI image has wrong platform"
    );
    ensure!(
        v["RepoDigests"]
            .as_array()
            .is_some_and(|values| values.iter().any(|v| v
                .as_str()
                .is_some_and(|s| canonical(s) == canonical(&d.image)))),
        "cached OCI image digest mismatch"
    );
    ensure!(
        v["Config"]["Volumes"]
            .as_object()
            .is_none_or(|v| v.is_empty()),
        "OCI image declares implicit writable volumes"
    );
    Ok(())
}

pub async fn clean(root: &Path) -> anyhow::Result<()> {
    if !root.join("oci-engine").exists() {
        return Ok(());
    }
    let bytes = output(
        public(),
        &args(&[
            "container",
            "ls",
            "-aq",
            "--filter",
            &format!("label={LABEL}={}", scope(root)),
        ]),
        20,
    )
    .await?;
    let ids = String::from_utf8(bytes)?;
    for id in ids.split_whitespace() {
        ensure!(
            id.bytes().all(|b| b.is_ascii_hexdigit()),
            "invalid OCI container ID"
        );
        output(public(), &args(&["container", "rm", "--force", id]), 20).await?;
    }
    Ok(())
}

/// Before native-process recovery, remove engine-owned processes left by a
/// dead daemon. Preserve all existing native containment/proof requirements.
pub fn clean_at_open(root: &Path) -> anyhow::Result<()> {
    if !root.join("oci-engine").exists() {
        return Ok(());
    }
    let root = root.to_owned();
    std::thread::spawn(move || {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?
            .block_on(clean(&root))
    })
    .join()
    .map_err(|_| anyhow::anyhow!("OCI recovery worker failed"))?
}

pub async fn start(root: &Path, workspace: &Path) -> anyhow::Result<tokio::process::Command> {
    let d = Descriptor::load(&workspace.join("artifact"))?;
    let expected = std::fs::read_to_string(workspace.join("artifact.sha256"))?;
    ensure!(
        crate::walrus_client::sha256_file(&workspace.join("artifact"))? == expected,
        "OCI descriptor digest mismatch"
    );
    verify(&d).await?;
    let container = name(workspace);
    // The marker is durable before any create can reach the engine.
    if !root.join("oci-engine").exists() {
        crate::configuration::write_private(
            &root.join("oci-engine"),
            &serde_json::json!({"version":1}),
        )?;
    }
    // Failed cleanup must prevent a second create/start, never overlap runs.
    let component = workspace
        .parent()
        .and_then(Path::parent)
        .context("invalid OCI workspace")?;
    if !component.join("oci-engine").exists() {
        crate::configuration::write_private(
            &component.join("oci-engine"),
            &serde_json::json!({"version":1}),
        )?;
    }
    clean_component(component).await?;
    let mut a = args(&[
        "container",
        "create",
        "--name",
        &container,
        "--label",
        &format!("{LABEL}={}", scope(root)),
        "--label",
        &format!("io.suifleet.component={}", scope(component)),
        "--platform",
        &d.platform,
        "--pull",
        "never",
        "--restart",
        "no",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        &d.user,
        "--network",
        &d.network,
        "--memory",
        &d.memory_bytes.to_string(),
        "--memory-swap",
        &d.memory_bytes.to_string(),
        "--cpus",
        &d.cpus.to_string(),
        "--pids-limit",
        &d.pids_limit.to_string(),
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=2",
        "--no-healthcheck",
    ]);
    if d.tmpfs_bytes > 0 {
        a.extend(args(&[
            "--tmpfs",
            &format!("/tmp:rw,noexec,nosuid,nodev,size={}", d.tmpfs_bytes),
        ]));
    }
    a.push(d.image);
    a.extend(d.command);
    output(public(), &a, 20).await?;
    let mut c = command(public());
    c.args(["container", "start", "--attach", &container]);
    Ok(c)
}
pub async fn clean_component(component: &Path) -> anyhow::Result<()> {
    #[cfg(test)]
    ensure!(
        !component.join("test-engine-unavailable").exists(),
        "injected OCI engine mutation outage"
    );
    let filter = format!("label=io.suifleet.component={}", scope(component));
    let bytes = output(
        public(),
        &args(&["container", "ls", "-aq", "--filter", &filter]),
        20,
    )
    .await?;
    for id in String::from_utf8(bytes)?.split_whitespace() {
        ensure!(
            id.bytes().all(|b| b.is_ascii_hexdigit()),
            "invalid OCI container ID"
        );
        output(public(), &args(&["container", "rm", "--force", id]), 20).await?;
    }
    Ok(())
}

pub async fn remove(workspace: &Path) -> anyhow::Result<()> {
    let container = name(workspace);
    let root = workspace
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .context("invalid OCI workspace")?;
    let bytes = output(
        public(),
        &args(&[
            "container",
            "ls",
            "-aq",
            "--filter",
            &format!("name=^{container}$"),
            "--filter",
            &format!("label={LABEL}={}", scope(root)),
        ]),
        20,
    )
    .await?;
    for id in String::from_utf8(bytes)?.split_whitespace() {
        ensure!(
            id.bytes().all(|b| b.is_ascii_hexdigit()),
            "invalid OCI container ID"
        );
        output(public(), &args(&["container", "rm", "--force", id]), 20).await?;
    }
    Ok(())
}
pub async fn ready(workspace: &Path) -> anyhow::Result<()> {
    let d = Descriptor::load(&workspace.join("artifact"))?;
    let container = name(workspace);
    let bytes = output(
        public(),
        &args(&[
            "container",
            "inspect",
            "--format",
            "{{.State.Running}}",
            &container,
        ]),
        10,
    )
    .await?;
    ensure!(bytes == b"true\n", "OCI container is not running");
    if !d.readiness.is_empty() {
        let mut a = args(&["container", "exec", &container]);
        a.extend(d.readiness);
        output(public(), &a, 10).await?;
    }
    Ok(())
}
pub async fn stop(workspace: &Path, seconds: u64) -> anyhow::Result<()> {
    #[cfg(test)]
    ensure!(
        !workspace
            .parent()
            .and_then(Path::parent)
            .context("invalid OCI workspace")?
            .join("test-engine-unavailable")
            .exists(),
        "injected OCI engine mutation outage"
    );
    output(
        public(),
        &args(&[
            "container",
            "stop",
            "--time",
            &seconds.to_string(),
            &name(workspace),
        ]),
        seconds + 5,
    )
    .await?;
    remove(workspace).await
}

#[cfg(all(
    test,
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64", target_arch = "arm")
))]
mod tests {
    use super::*;
    fn descriptor() -> Descriptor {
        serde_json::from_value(serde_json::json!({"version":1,"image":format!("example.com/app@sha256:{}", "a".repeat(64)),"platform":host_platform(),"user":"1000:1000","memoryBytes":67108864u64,"cpus":1,"pidsLimit":32,"network":"none","tmpfsBytes":1048576})).unwrap()
    }
    #[test]
    fn rejects_mutable_images_and_escape_flags() {
        let mut d = descriptor();
        assert!(d.validate().is_ok());
        for image in [
            "alpine:latest",
            "--privileged",
            "example.com/app@sha256:bad",
        ] {
            d.image = image.into();
            assert!(d.validate().is_err());
        }
        d = descriptor();
        d.network = "host".into();
        assert!(d.validate().is_err());
        d = descriptor();
        d.user = "0".into();
        assert!(d.validate().is_err());
        d = descriptor();
        d.platform = "unsupported".into();
        assert!(d.validate().is_err());
        d = descriptor();
        d.registry_auth = Some("../../config".into());
        assert!(d.validate().is_err());
        d = descriptor();
        d.memory_bytes = 0;
        assert!(d.validate().is_err());
        d = descriptor();
        d.tmpfs_bytes = d.memory_bytes + 1;
        assert!(d.validate().is_err());
    }
    #[test]
    fn identity_is_scope_and_workspace_specific() {
        assert_ne!(name(Path::new("/a/v1")), name(Path::new("/a/v2")));
        assert_ne!(scope(Path::new("/a")), scope(Path::new("/b")));
    }
    #[test]
    fn auth_profile_cannot_execute_credential_plugins() {
        let path = std::env::temp_dir().join(format!("sf036-auth-{}.json", std::process::id()));
        for (value, valid) in [
            (
                serde_json::json!({"auths":{"example.com":{"auth":""}}}),
                true,
            ),
            (serde_json::json!({"credsStore":"executable"}), false),
            (
                serde_json::json!({"auths":{},"credHelpers":{"example.com":"executable"}}),
                false,
            ),
            (serde_json::json!([]), false),
        ] {
            std::fs::write(&path, serde_json::to_vec(&value).unwrap()).unwrap();
            assert_eq!(
                validate_auth(std::fs::File::open(&path).unwrap()).is_ok(),
                valid
            );
        }
        std::fs::remove_file(path).unwrap();
    }
    #[test]
    fn credential_files_reject_shared_permissions_and_symlinks() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let base = std::env::temp_dir().join(format!("sf036-profile-{}", std::process::id()));
        let profile = base.join("test");
        std::fs::create_dir_all(&profile).unwrap();
        let path = profile.join("config.json");
        std::fs::write(&path, b"{\"auths\":{}}").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        let mut d = descriptor();
        d.registry_auth = Some("test".into());
        assert!(d.config_at(&base).is_ok());
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o640)).unwrap();
        assert!(d.config_at(&base).is_err());
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::rename(&path, profile.join("actual.json")).unwrap();
        symlink(profile.join("actual.json"), &path).unwrap();
        assert!(d.config_at(&base).is_err());
        std::fs::remove_file(&path).unwrap();
        std::fs::rename(profile.join("actual.json"), &path).unwrap();
        std::fs::set_permissions(&profile, std::fs::Permissions::from_mode(0o777)).unwrap();
        assert!(d.config_at(&base).is_err());
        std::fs::remove_dir_all(base).unwrap();
    }
    #[tokio::test]
    #[ignore = "requires local Docker Engine"]
    async fn oci_real_engine_forwards_private_registry_profile_and_rejects_bad_auth() {
        use base64::Engine;
        use std::{
            io::{Read, Write},
            sync::{
                atomic::{AtomicBool, Ordering},
                Arc,
            },
        };
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let registry = format!("127.0.0.1:{}", listener.local_addr().unwrap().port());
        // Deliberately invalid fixture credentials; no provider account exists.
        let encoded =
            base64::engine::general_purpose::STANDARD.encode("sf036-fixture:invalid-fixture-value");
        let seen = Arc::new(AtomicBool::new(false));
        let done = Arc::new(AtomicBool::new(false));
        let (saw_auth, stop) = (seen.clone(), done.clone());
        let expected = format!("authorization: basic {}", encoded).to_ascii_lowercase();
        let server = std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                if let Ok((mut stream, _)) = listener.accept() {
                    stream
                        .set_read_timeout(Some(Duration::from_secs(1)))
                        .unwrap();
                    let mut buffer = [0u8; 8192];
                    if let Ok(n) = stream.read(&mut buffer) {
                        if String::from_utf8_lossy(&buffer[..n])
                            .to_ascii_lowercase()
                            .contains(&expected)
                        {
                            saw_auth.store(true, Ordering::SeqCst);
                        }
                    }
                    let _ = stream.write_all(b"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Basic realm=\"test\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                } else {
                    std::thread::sleep(Duration::from_millis(10));
                }
            }
        });
        let base = std::env::temp_dir().join(format!("sf036-auth-engine-{}", std::process::id()));
        let profile = base.join("fixture");
        std::fs::create_dir_all(&profile).unwrap();
        crate::configuration::write_private(
            &profile.join("config.json"),
            &serde_json::json!({"auths":{(registry.clone()):{"auth":encoded}}}),
        )
        .unwrap();
        let mut d = descriptor();
        d.registry_auth = Some("fixture".into());
        let config = d.config_at(&base).unwrap();
        let image = format!("{registry}/test/app@sha256:{}", "a".repeat(64));
        let result = output(
            &config,
            &args(&["image", "pull", "--platform", host_platform(), &image]),
            30,
        )
        .await;
        done.store(true, Ordering::SeqCst);
        server.join().unwrap();
        std::fs::remove_dir_all(base).unwrap();
        assert!(result.is_err(), "bad registry credentials were accepted");
        assert!(
            seen.load(Ordering::SeqCst),
            "configured fixture credentials never reached the registry"
        );
        let error = result.unwrap_err().to_string();
        assert!(
            error.contains("diagnostics withheld"),
            "credential failure was not sanitized"
        );
    }
}
