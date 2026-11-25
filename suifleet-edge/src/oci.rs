//! Local Docker and Podman OCI adapter. Host grants require Docker. Arbitrary
//! flags, inherited credentials and engine-managed restarts are forbidden.
use anyhow::{ensure, Context};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

pub const RUN: &str = "suifleet:oci";
const LABEL: &str = "io.suifleet.supervisor";
// @ is forbidden in component path segments, so this cannot hide a component.
const ENGINE_MARKER: &str = "@oci-engine";
const ANONYMOUS_AUTH: &str = "@oci-anonymous-auth.json";

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
    #[serde(default)]
    persistence: Option<Retention>,
    #[serde(default)]
    devices: Vec<String>,
    #[serde(default)]
    ipc: bool,
}
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
enum Retention {
    Retain,
}

/// Host policy. Recipes can request grants but cannot supply paths or identities.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    uid: u32,
    #[serde(default)]
    persistence: bool,
    #[serde(default)]
    devices: Vec<String>,
    #[serde(default)]
    ipc: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Policy {
    pub grants: BTreeMap<String, Grant>,
    pub ipc: Option<crate::ipc::Settings>,
}

impl Policy {
    pub fn validate(&self) -> anyhow::Result<()> {
        let mut uids = BTreeSet::new();
        let mut devices = BTreeSet::new();
        for (name, grant) in &self.grants {
            ensure!(
                crate::deps::valid_name(name) && name != "." && name != "..",
                "invalid OCI grant component"
            );
            ensure!(
                grant.uid != 0 && grant.uid != unsafe { libc::geteuid() } && uids.insert(grant.uid),
                "OCI grants require distinct non-root, non-daemon UIDs"
            );
            ensure!(
                grant.devices.len() <= 8
                    && grant
                        .devices
                        .iter()
                        .all(|name| device_number(name).is_some() && devices.insert(name)),
                "invalid or overlapping OCI device grant"
            );
            if grant.ipc {
                ensure!(
                    self.ipc
                        .as_ref()
                        .and_then(|ipc| ipc.components.get(name))
                        .is_some_and(|acl| acl.uid == grant.uid),
                    "OCI IPC grant requires a matching local IPC component UID"
                );
            }
            if let Some(ipc) = &self.ipc {
                ensure!(
                    !ipc.administrators.contains(&grant.uid)
                        && !ipc
                            .components
                            .iter()
                            .any(|(other, acl)| other != name && acl.uid == grant.uid),
                    "OCI grant UID belongs to another IPC principal"
                );
            }
        }
        Ok(())
    }

    async fn engine_args(
        &self,
        engine: Engine,
        d: &Descriptor,
        root: &Path,
        component: &str,
    ) -> anyhow::Result<Vec<String>> {
        let mut result = Vec::new();
        if d.persistence.is_none() && d.devices.is_empty() && !d.ipc {
            return Ok(result);
        }
        ensure!(
            engine == Engine::Docker,
            "OCI persistence, device and IPC grants require Docker"
        );
        let grant = self
            .grants
            .get(component)
            .context("missing local OCI grant")?;
        ensure!(
            d.user == format!("{0}:{0}", grant.uid),
            "OCI grant requires its local UID:GID"
        );
        ensure!(
            (d.persistence.is_none() || grant.persistence)
                && d.devices.iter().all(|name| grant.devices.contains(name))
                && (!d.ipc || grant.ipc),
            "OCI capability is not granted on this device"
        );

        // Host ownership and SO_PEERCRED must refer to the same numeric IDs
        // as Docker's explicit USER. Do not override engine user namespaces.
        let security: Vec<String> = serde_json::from_slice(
            &output(
                engine,
                public(),
                &args(&["info", "--format", "{{json .SecurityOptions}}"]),
                20,
            )
            .await?,
        )?;
        ensure!(
            !security.iter().any(|s| s
                .split(',')
                .any(|part| matches!(part, "name=userns" | "name=rootless"))),
            "OCI grants require Docker without user namespace remapping or rootless mode"
        );

        if d.persistence.is_some() {
            let state = root.parent().context("missing OCI state directory")?;
            let state_metadata = std::fs::symlink_metadata(state)?;
            let daemon_uid = unsafe { libc::geteuid() };
            ensure!(
                state_metadata.is_dir()
                    && (state_metadata.uid() == daemon_uid || state_metadata.uid() == 0)
                    && state_metadata.mode() & 0o022 == 0,
                "OCI state directory must be trusted and not group/world writable"
            );
            let base = state.join("oci-data");
            let parent = std::fs::symlink_metadata(&base)?;
            let source = base.join(component);
            let data = std::fs::symlink_metadata(&source)?;
            ensure!(
                parent.is_dir()
                    && parent.uid() == daemon_uid
                    && parent.mode() & 0o7777 == 0o700
                    && data.is_dir()
                    && data.uid() == grant.uid
                    && data.gid() == grant.uid
                    && data.mode() & 0o7777 == 0o700,
                "OCI data directory has unsafe ownership, type or permissions"
            );
            bind_arg(&mut result, &source, "/var/lib/suifleet-data", false)?;
        }
        for name in &d.devices {
            let source = Path::new("/dev").join(name);
            let metadata = std::fs::symlink_metadata(&source)?;
            ensure!(
                metadata.file_type().is_char_device()
                    && device_number(name)
                        .is_some_and(|number| device_matches(metadata.rdev(), number)),
                "OCI grant does not resolve to the expected character device"
            );
            result.extend(args(&[
                "--device",
                &format!("/dev/{name}:/dev/suifleet-{name}:rw"),
            ]));
        }
        if d.ipc {
            let socket = &self.ipc.as_ref().context("missing OCI IPC service")?.socket;
            let metadata = std::fs::symlink_metadata(socket)?;
            ensure!(
                metadata.file_type().is_socket() && metadata.uid() == unsafe { libc::geteuid() },
                "OCI IPC path is not the daemon socket"
            );
            bind_arg(&mut result, socket, "/run/suifleet-ipc.sock", true)?;
        }
        Ok(result)
    }
}

fn bind_arg(
    args: &mut Vec<String>,
    source: &Path,
    target: &str,
    readonly: bool,
) -> anyhow::Result<()> {
    let source = source.to_str().context("OCI mount path must be UTF-8")?;
    ensure!(
        Path::new(source).is_absolute()
            && !source
                .chars()
                .any(|c| c.is_control() || matches!(c, ',' | '"')),
        "unsafe OCI mount path"
    );
    args.push("--mount".into());
    let mode = if readonly { ",readonly" } else { "" };
    args.push(format!(
        "type=bind,source={source},target={target},bind-recursive=disabled{mode}"
    ));
    Ok(())
}

fn device_number(name: &str) -> Option<(u32, u32)> {
    #[cfg(test)]
    if name == "full" {
        return Some((1, 7));
    }
    [("ttyUSB", 188), ("ttyACM", 166)]
        .into_iter()
        .find_map(|(prefix, major)| {
            let suffix = name.strip_prefix(prefix)?;
            let number: u8 = suffix.parse().ok()?;
            (suffix == number.to_string()).then_some((major, u32::from(number)))
        })
}

#[cfg(target_os = "linux")]
fn device_matches(rdev: u64, number: (u32, u32)) -> bool {
    (libc::major(rdev), libc::minor(rdev)) == number
}

#[cfg(not(target_os = "linux"))]
fn device_matches(_rdev: u64, _number: (u32, u32)) -> bool {
    false
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
            cfg!(target_os = "linux")
                && host_platform() != "unsupported"
                && matches!(self.version, 1 | 2),
            "unsupported OCI platform/schema"
        );
        ensure!(
            self.version == 2
                || (self.persistence.is_none() && self.devices.is_empty() && !self.ipc),
            "OCI capability requests require descriptor version 2"
        );
        let devices: BTreeSet<_> = self.devices.iter().collect();
        ensure!(
            devices.len() == self.devices.len()
                && devices.len() <= 8
                && devices.iter().all(|name| device_number(name).is_some()),
            "invalid OCI device request"
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
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, serde::Serialize)]
#[serde(rename_all = "kebab-case")]
enum Engine {
    #[default]
    Docker,
    PodmanRootful,
    PodmanRootless,
}

#[derive(Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
struct EngineMarker {
    version: u8,
    #[serde(default)]
    engine: Engine,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    rootless_uid: Option<u32>,
}

impl Engine {
    fn selected(root: &Path) -> anyhow::Result<Self> {
        let engine = match std::env::var("SUIFLEET_OCI_ENGINE").as_deref() {
            Err(std::env::VarError::NotPresent) | Ok("docker") => Self::Docker,
            Ok("podman-rootful") => Self::PodmanRootful,
            Ok("podman-rootless") => Self::PodmanRootless,
            _ => anyhow::bail!("unsupported SUIFLEET_OCI_ENGINE selection"),
        };
        match std::fs::read(root.join(ENGINE_MARKER)) {
            Ok(bytes) => {
                let marker: EngineMarker = serde_json::from_slice(&bytes)?;
                ensure!(marker.version == 1, "unsupported OCI engine marker");
                ensure!(
                    engine == marker.engine && engine.rootless_uid() == marker.rootless_uid,
                    "OCI engine differs from durable recovery marker; restore the prior engine and retire its components first"
                );
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        Ok(engine)
    }

    fn rootless_uid(self) -> Option<u32> {
        (self == Self::PodmanRootless).then(|| unsafe { libc::geteuid() })
    }

    fn command(self, config: &Path) -> tokio::process::Command {
        let mut command;
        match self {
            Self::Docker => {
                command = tokio::process::Command::new("/usr/bin/docker");
                command
                    .args(["--host", "unix:///var/run/docker.sock", "--config"])
                    .arg(config);
            }
            Self::PodmanRootful | Self::PodmanRootless => {
                let url = if self == Self::PodmanRootless {
                    "unix:///run/suifleet-podman/podman.sock"
                } else {
                    "unix:///run/podman/podman.sock"
                };
                // The socket-only binary runs inside the daemon's sandbox.
                command = tokio::process::Command::new("/usr/bin/podman-remote");
                command.args(["--url", url]);
            }
        }
        crate::apply::harden(&mut command, Path::new("/"), None);
        if self != Self::Docker {
            // Pulls always use an explicit auth file. Keep ordinary client
            // configuration discovery out of the daemon account's home.
            command
                .env("HOME", "/proc/self/fd")
                .env("XDG_CONFIG_HOME", "/proc/self/fd")
                .env("DOCKER_CONFIG", "/proc/self/fd");
            // The client initializes a libpod runtime path for non-root
            // callers, even with a rootful engine on the other end.
            command.env("XDG_RUNTIME_DIR", "/run/suifleet-podman");
        }
        command
    }

    async fn check(self, descriptor: &Descriptor) -> anyhow::Result<()> {
        if self == Self::Docker {
            return Ok(());
        }
        ensure!(
            descriptor.platform != "linux/arm/v7",
            "Podman OCI does not support linux/arm/v7: image inspect cannot verify the ARM variant"
        );
        ensure!(
            self != Self::PodmanRootless || descriptor.network == "none",
            "rootless Podman supports only OCI network none"
        );
        let bytes = output(self, public(), &args(&["info", "--format", "json"]), 20).await?;
        let info: serde_json::Value = serde_json::from_slice(&bytes)?;
        let host = &info["host"];
        ensure!(
            host["security"]["rootless"].as_bool() == Some(self == Self::PodmanRootless),
            "Podman socket mode does not match the selected OCI engine"
        );
        ensure!(
            host["cgroupVersion"] == "v2"
                && host["cgroupControllers"]
                    .as_array()
                    .is_some_and(|controllers| ["cpu", "memory", "pids"]
                        .iter()
                        .all(|name| controllers.iter().any(|entry| entry == *name))),
            "Podman OCI requires cgroup v2 with delegated cpu, memory and pids controllers"
        );
        ensure!(
            host["security"]["seccompEnabled"] == true,
            "Podman OCI requires engine seccomp support"
        );
        Ok(())
    }
}

fn workspace_root(workspace: &Path) -> anyhow::Result<&Path> {
    workspace
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .context("invalid OCI workspace")
}

#[cfg(test)]
pub(crate) fn test_command(root: &Path) -> std::process::Command {
    Engine::selected(root).unwrap().command(public()).into_std()
}

async fn output(
    engine: Engine,
    config: &Path,
    args: &[String],
    seconds: u64,
) -> anyhow::Result<Vec<u8>> {
    if engine == Engine::Docker && config == public() && config.join("config.json").exists() {
        let value: serde_json::Value =
            serde_json::from_slice(&std::fs::read(config.join("config.json"))?)?;
        ensure!(
            value.as_object().is_some_and(|v| v.is_empty()),
            "anonymous OCI config must be empty"
        );
    }
    let mut c = engine.command(config);
    c.args(args).stdout(Stdio::piped()).stderr(Stdio::null());
    // A readiness/select cancellation must not abandon a registered helper.
    // The bounded task completes/reaps independently if its caller goes away.
    tokio::spawn(async move {
        use tokio::io::AsyncReadExt;
        let mut child = crate::supervisor::spawn_tracked(&mut c)?;
        let mut stdout = child
            .take_stdout()
            .context("missing OCI engine output")?
            .take(1024 * 1024 + 1);
        let mut reaped = false;
        let result = tokio::time::timeout(Duration::from_secs(seconds), async {
            let mut bytes = Vec::new();
            stdout.read_to_end(&mut bytes).await?;
            ensure!(
                bytes.len() <= 1024 * 1024,
                "OCI engine response exceeds limit"
            );
            let status = child.wait().await?;
            reaped = true;
            ensure!(
                status.success(),
                "OCI engine operation failed (credentials and engine diagnostics withheld)"
            );
            Ok(bytes)
        })
        .await
        .unwrap_or_else(|_| Err(anyhow::anyhow!("OCI engine timed out")));
        if !reaped {
            crate::apply::kill_group(child.pid() as i32);
            let _ = child.wait().await;
        }
        result
    })
    .await
    .context("OCI engine helper task failed")?
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
pub async fn prepare(
    artifact: &Path,
    root: &Path,
    component: &str,
    policy: &Policy,
) -> anyhow::Result<()> {
    let d = Descriptor::load(artifact)?;
    let engine = Engine::selected(root)?;
    policy.engine_args(engine, &d, root, component).await?;
    engine.check(&d).await?;
    if verify(engine, &d).await.is_err() {
        let config = d.config()?;
        let mut pull = args(&["image", "pull", "--platform", &d.platform]);
        if engine != Engine::Docker {
            pull.extend(args(&["--tls-verify=true"]));
            let auth = if d.registry_auth.is_some() {
                config.join("config.json")
            } else {
                let auth = root.join(ANONYMOUS_AUTH);
                crate::configuration::write_private(&auth, &serde_json::json!({}))?;
                auth
            };
            pull.push("--authfile".into());
            pull.push(
                auth.to_str()
                    .context("OCI auth path is not UTF-8")?
                    .to_owned(),
            );
        }
        pull.push(d.image.clone());
        output(engine, &config, &pull, 120).await?;
        verify(engine, &d).await?;
    }
    Ok(())
}
async fn verify(engine: Engine, d: &Descriptor) -> anyhow::Result<()> {
    let bytes = output(engine, public(), &args(&["image", "inspect", &d.image]), 20).await?;
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
    if !root.join(ENGINE_MARKER).exists() {
        return Ok(());
    }
    let engine = Engine::selected(root)?;
    let bytes = output(
        engine,
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
        output(
            engine,
            public(),
            &args(&["container", "rm", "--force", id]),
            20,
        )
        .await?;
    }
    // Only a confirmed global cleanup can discharge the durable engine
    // obligation. Retained versions alone do not require a running engine.
    if !has_components(root)? {
        std::fs::remove_file(root.join(ENGINE_MARKER))?;
    }
    Ok(())
}

fn has_components(root: &Path) -> anyhow::Result<bool> {
    Ok(std::fs::read_dir(root)?.any(|entry| {
        entry
            .map(|entry| entry.path().join("oci-engine").exists())
            .unwrap_or(true)
    }))
}

/// Retirement must never clean containers belonging to another active component.
pub async fn release_if_unused(root: &Path) -> anyhow::Result<()> {
    if !has_components(root)? {
        clean(root).await?;
    }
    Ok(())
}

/// Before native-process recovery, remove engine-owned processes left by a
/// dead daemon. Preserve all existing native containment/proof requirements.
pub fn clean_at_open(root: &Path) -> anyhow::Result<()> {
    if !root.join(ENGINE_MARKER).exists() {
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

pub async fn start(
    root: &Path,
    workspace: &Path,
    policy: &Policy,
) -> anyhow::Result<tokio::process::Command> {
    let d = Descriptor::load(&workspace.join("artifact"))?;
    let expected = std::fs::read_to_string(workspace.join("artifact.sha256"))?;
    ensure!(
        crate::walrus_client::sha256_file(&workspace.join("artifact"))? == expected,
        "OCI descriptor digest mismatch"
    );
    let component = workspace
        .parent()
        .and_then(Path::parent)
        .context("invalid OCI workspace")?;
    let component_name = component
        .file_name()
        .and_then(|name| name.to_str())
        .context("invalid OCI component name")?;
    let engine = Engine::selected(root)?;
    let grants = policy.engine_args(engine, &d, root, component_name).await?;
    engine.check(&d).await?;
    verify(engine, &d).await?;
    let container = name(workspace);
    // The marker is durable before any create can reach the engine.
    if !root.join(ENGINE_MARKER).exists() {
        crate::configuration::write_private(
            &root.join(ENGINE_MARKER),
            &EngineMarker {
                version: 1,
                engine,
                rootless_uid: engine.rootless_uid(),
            },
        )?;
    }
    // Failed cleanup must prevent a second create/start, never overlap runs.
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
        "--no-healthcheck",
    ]);
    if engine == Engine::Docker {
        a.extend(args(&[
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=2",
        ]));
    } else {
        a.extend(args(&[
            "--read-only-tmpfs=false",
            "--image-volume=ignore",
            "--log-driver=k8s-file",
            "--log-opt=max-size=1m",
            "--cgroups=enabled",
            "--http-proxy=false",
            "--systemd=false",
        ]));
    }
    if d.tmpfs_bytes > 0 {
        a.extend(args(&[
            "--tmpfs",
            &format!("/tmp:rw,noexec,nosuid,nodev,size={}", d.tmpfs_bytes),
        ]));
    }
    a.extend(grants);
    a.push(d.image);
    a.extend(d.command);
    output(engine, public(), &a, 20)
        .await
        .context("OCI engine rejected container creation with the required isolation and limits")?;
    let mut c = engine.command(public());
    c.args(["container", "start", "--attach", &container]);
    Ok(c)
}
pub async fn clean_component(component: &Path) -> anyhow::Result<()> {
    #[cfg(test)]
    ensure!(
        !component.join("test-engine-unavailable").exists(),
        "injected OCI engine mutation outage"
    );
    let root = component.parent().context("invalid OCI component")?;
    let engine = Engine::selected(root)?;
    let filter = format!("label=io.suifleet.component={}", scope(component));
    let bytes = output(
        engine,
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
        output(
            engine,
            public(),
            &args(&["container", "rm", "--force", id]),
            20,
        )
        .await?;
    }
    Ok(())
}

pub async fn remove(workspace: &Path) -> anyhow::Result<()> {
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
    let container = name(workspace);
    let root = workspace_root(workspace)?;
    let engine = Engine::selected(root)?;
    let bytes = output(
        engine,
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
        output(
            engine,
            public(),
            &args(&["container", "rm", "--force", id]),
            20,
        )
        .await?;
    }
    Ok(())
}
pub async fn ready(workspace: &Path) -> anyhow::Result<()> {
    let engine = Engine::selected(workspace_root(workspace)?)?;
    let d = Descriptor::load(&workspace.join("artifact"))?;
    let container = name(workspace);
    let bytes = output(
        engine,
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
        output(engine, public(), &a, 10).await?;
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
    let engine = Engine::selected(workspace_root(workspace)?)?;
    output(
        engine,
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
    fn legacy_recovery_marker_selects_docker() {
        let marker: EngineMarker = serde_json::from_str(r#"{"version":1}"#).unwrap();
        assert_eq!(marker.engine, Engine::Docker);
        assert!(marker.rootless_uid.is_none());
    }

    #[tokio::test]
    async fn rejects_podman_armv7_before_engine_operation() {
        let mut descriptor = descriptor();
        descriptor.platform = "linux/arm/v7".to_owned();
        for engine in [Engine::PodmanRootful, Engine::PodmanRootless] {
            assert_eq!(
                engine.check(&descriptor).await.unwrap_err().to_string(),
                "Podman OCI does not support linux/arm/v7: image inspect cannot verify the ARM variant"
            );
        }
        assert!(Engine::Docker.check(&descriptor).await.is_ok());
    }

    #[tokio::test]
    async fn podman_rejects_docker_grants_before_engine_operation() {
        let policy = Policy::default();
        let root = Path::new("/unused");
        for engine in [Engine::PodmanRootful, Engine::PodmanRootless] {
            let mut d = descriptor();
            d.version = 2;
            assert!(policy
                .engine_args(engine, &d, root, "app")
                .await
                .unwrap()
                .is_empty());
            for (persistence, devices, ipc) in [
                (Some(Retention::Retain), vec![], false),
                (None, vec!["ttyUSB0".into()], false),
                (None, vec![], true),
            ] {
                d.persistence = persistence;
                d.devices = devices;
                d.ipc = ipc;
                let error = policy
                    .engine_args(engine, &d, root, "app")
                    .await
                    .unwrap_err();
                assert!(error.to_string().contains("grants require Docker"));
            }
        }
    }

    #[tokio::test]
    async fn rejects_rootless_bridge_before_engine_operation() {
        let mut descriptor = descriptor();
        descriptor.platform = "linux/amd64".to_owned();
        descriptor.network = "bridge".to_owned();
        assert!(Engine::PodmanRootless
            .check(&descriptor)
            .await
            .unwrap_err()
            .to_string()
            .contains("supports only OCI network none"));
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
    fn v2_rejects_arbitrary_sources_and_duplicate_requests() {
        let mut d = descriptor();
        d.persistence = Some(Retention::Retain);
        assert!(d.validate().is_err());
        d.version = 2;
        for name in [
            "/dev/ttyUSB0",
            "../ttyUSB0",
            "ttyUSB00",
            "ttyUSB256",
            "sda",
            "mem",
            "docker.sock",
        ] {
            d.devices = vec![name.into()];
            assert!(d.validate().is_err(), "{name}");
        }
        d.devices = vec!["ttyUSB0".into(), "ttyUSB0".into()];
        assert!(d.validate().is_err());
        d.devices = vec!["ttyUSB0".into(), "ttyACM1".into()];
        assert!(d.validate().is_ok());
        let device = device_number("ttyUSB0").unwrap();
        assert!(device_matches(libc::makedev(188, 0), device));
        assert!(!device_matches(libc::makedev(188, 1), device));
        assert!(!device_matches(libc::makedev(1, 0), device));
        for source in ["/state/a,readonly", "/state/a\n", "/state/a\"b", "relative"] {
            assert!(bind_arg(&mut Vec::new(), Path::new(source), "/target", false).is_err());
        }
    }

    #[test]
    fn policy_rejects_shared_devices_and_ipc_principals() {
        let grant = |uid, device: &str, ipc| Grant {
            uid,
            devices: vec![device.into()],
            persistence: false,
            ipc,
        };
        let mut policy = Policy {
            grants: BTreeMap::from([
                ("app".into(), grant(31001, "ttyUSB0", false)),
                ("other".into(), grant(31002, "ttyUSB0", false)),
            ]),
            ipc: None,
        };
        assert!(policy.validate().is_err());
        policy.grants.get_mut("other").unwrap().devices.clear();
        assert!(policy.validate().is_ok());
        policy.grants.get_mut("other").unwrap().uid = 31001;
        assert!(policy.validate().is_err());
        policy.grants.remove("other");
        policy.grants.get_mut("app").unwrap().ipc = true;
        assert!(policy.validate().is_err());
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
        use base64::Engine as _;
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
            Engine::Docker,
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
    #[tokio::test]
    #[ignore = "requires local Docker Engine"]
    async fn oci_real_engine_reaps_timed_out_and_cancelled_helpers() {
        let filter = format!("label=io.suifleet.cancel-test={}", std::process::id());
        let command = args(&["events", "--filter", &filter]);
        assert!(output(Engine::Docker, public(), &command, 1)
            .await
            .unwrap_err()
            .to_string()
            .contains("timed out"));
        let cancelled =
            tokio::spawn(async move { output(Engine::Docker, public(), &command, 1).await });
        tokio::time::sleep(Duration::from_millis(200)).await;
        cancelled.abort();
        let _ = cancelled.await;
        tokio::time::sleep(Duration::from_millis(1300)).await;
        // Check actual live client processes, rather than mock a cleanup result.
        for entry in std::fs::read_dir("/proc").unwrap().flatten() {
            if let Ok(bytes) = std::fs::read(entry.path().join("cmdline")) {
                assert!(
                    !bytes.windows(filter.len()).any(|w| w == filter.as_bytes()),
                    "cancelled engine helper survived its deadline"
                );
            }
        }
    }
    #[test]
    fn native_component_directory_does_not_enable_engine_recovery() {
        let root = std::env::temp_dir().join(format!("sf036-marker-{}", std::process::id()));
        std::fs::create_dir_all(root.join("oci-engine")).unwrap();
        assert!(!crate::deps::valid_name(ENGINE_MARKER));
        clean_at_open(&root).unwrap();
        std::fs::remove_dir_all(root).unwrap();
    }
}
