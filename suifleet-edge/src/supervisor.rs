//! Supervised component runtime (SF-014): persistent versioned workspaces,
//! process lifecycle, readiness, restart policy and descendant containment.
//!
//! Layout under `<state_dir>/supervised` (apart from the dependency cache in
//! `<state_dir>/components`, so no component name can collide with it):
//! - `active.json`: the committed version of each component, started again on
//!   every daemon start (bootstrap, reboot). A version is committed only once it
//!   became ready, so an interrupted replacement restarts the previous one.
//! - `status.json`: genuine runtime state, rewritten on every transition.
//! - `<name>/versions/<version>-<deployment>/`: the version's verified artifact
//!   (and whatever its install step wrote). The active and previous versions
//!   are kept for rollback; older ones are removed.
//! - `<name>/work/`: the component's working directory and HOME, kept across
//!   versions; `<name>/component.log` (0600): the stdout/stderr of all its
//!   commands (install and validate too, also for an install-only component),
//!   which see its configuration and so stay out of the daemon's output.
//! - In a version's workspace, `configuration-state.json`: the public
//!   configuration record it was deployed with (SF-015), and
//!   `configuration.json` (0600): that configuration with the device overlay,
//!   rewritten before every start so overlay changes apply on restart. Its
//!   commands see it as `{configuration}` and `$SUIFLEET_CONFIGURATION`; a
//!   rollback restores the previous version with its own configuration.
//!
//! Run commands must stay in the foreground. Each runs in its own process
//! group, which is killed when it exits or stops; the daemon is also a child
//! subreaper and kills any process that escaped its group and was orphaned.
//! Component processes carry a marker in their environment, so a daemon
//! restarted after a crash also finds and kills ones reparented away from it.
//! `instance.json` records the daemon supervising this state root until it
//! stopped every component, and is marked before it starts the first component
//! process (install or run). A daemon that finds the marked record of one that
//! did not stop them, and cannot prove that none of its processes survived (a
//! sealed cgroup with nothing left in it, or a reboot), refuses to start.
use crate::{
    apply::{self, ApplyRunner},
    configuration,
    platform::{processes, start_time, Proc},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::ExitStatus,
    sync::{Arc, Mutex},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::watch;
use tracing::{info, warn};

const MAX_BACKOFF: Duration = Duration::from_secs(60);
const RESTART_WINDOW: Duration = Duration::from_secs(600);
const STABLE_RUN: Duration = Duration::from_secs(60);
const READINESS_ATTEMPT: Duration = Duration::from_secs(10);
const LOG_ROTATE_BYTES: u64 = 1024 * 1024;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Restart {
    #[default]
    OnFailure,
    Never,
    Always,
}
impl Restart {
    pub fn parse(value: Option<&str>) -> anyhow::Result<Self> {
        Ok(match value {
            None | Some("on-failure") => Self::OnFailure,
            Some("never") => Self::Never,
            Some("always") => Self::Always,
            Some(other) => anyhow::bail!("unsupported restart policy {other:?}"),
        })
    }
}

/// A recipe's supervised lifecycle (present when it has a run command).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Lifecycle {
    pub run: String,
    pub shutdown: Option<String>,
    pub readiness: Option<String>,
    pub restart: Restart,
}
impl Lifecycle {
    pub fn of(lifecycle: &crate::protocol::Lifecycle) -> anyhow::Result<Option<Self>> {
        let Some(run) = &lifecycle.run else {
            return Ok(None);
        };
        Ok(Some(Self {
            run: run.clone(),
            shutdown: lifecycle.shutdown.clone(),
            readiness: lifecycle.readiness.clone(),
            restart: Restart::parse(lifecycle.restart.as_deref())?,
        }))
    }
}

/// One supervised component version, as committed in `active.json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Spec {
    pub name: String,
    pub version: String,
    pub deployment: String,
    pub run: String,
    pub shutdown: Option<String>,
    pub readiness: Option<String>,
    pub restart: Restart,
    /// Components this one depends on; started (and ready) first at bootstrap.
    #[serde(default)]
    pub after: Vec<String>,
}
impl Spec {
    pub fn new(name: &str, version: &str, deployment: &str, lifecycle: Lifecycle, after: Vec<String>) -> Self {
        Self {
            name: name.to_owned(),
            version: version.to_owned(),
            deployment: deployment.to_owned(),
            run: lifecycle.run,
            shutdown: lifecycle.shutdown,
            readiness: lifecycle.readiness,
            restart: lifecycle.restart,
            after,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Phase {
    Starting,
    Running,
    Backoff,
    Stopping,
    Stopped,
    /// Ended and not restarted, per its restart policy.
    Exited,
    /// Restart budget exhausted.
    Broken,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Status {
    pub version: String,
    pub deployment: String,
    pub phase: Phase,
    pub pid: Option<u32>,
    /// Kernel start time of `pid` (clock ticks since boot), so a later daemon
    /// can recognise a run it left behind without mistaking a reused PID.
    #[serde(default)]
    pub pid_start: Option<u64>,
    pub restarts: u32,
    pub last_exit: Option<String>,
    /// Unix seconds of the last transition.
    pub since: u64,
}

#[derive(Debug, Clone)]
pub struct Settings {
    /// Without a readiness command, a run that survives this long is ready.
    pub startup: Duration,
    pub ready_timeout: Duration,
    pub stop_timeout: Duration,
    /// Restarts allowed within ten minutes before the component is Broken.
    pub max_restarts: u32,
    pub max_memory_bytes: Option<u64>,
    /// Device-local configuration values (configuration.rs).
    pub overlay_dir: PathBuf,
}

const CONFIGURATION_STATE: &str = "configuration-state.json";
const CONFIGURATION: &str = "configuration.json";

struct Handle {
    stop: watch::Sender<bool>,
    phase: watch::Receiver<Phase>,
    task: tokio::task::JoinHandle<()>,
}

/// A failed replacement whose previous application was explicitly restored
/// and passed readiness. Only this evidence may restore journal confirmation.
#[derive(Debug)]
pub struct Recovered(pub String);
impl std::fmt::Display for Recovered {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str(&self.0) }
}
impl std::error::Error for Recovered {}

pub struct Supervisor {
    root: PathBuf,
    settings: Settings,
    active: BTreeMap<String, Spec>,
    running: BTreeMap<String, Handle>,
    status: Arc<Mutex<BTreeMap<String, Status>>>,
    record: Record,
    recovery_commands: BTreeMap<String, String>,
    previous: BTreeMap<String, Spec>,
    recovered: BTreeMap<String, String>,
}

impl Supervisor {
    pub fn open(state_dir: &Path, settings: Settings) -> anyhow::Result<Self> {
        let root = state_dir.join("supervised");
        std::fs::create_dir_all(&root)?;
        anyhow::ensure!(
            !root.join("recovery-intent.json").exists(),
            "application recovery was interrupted or failed; inspect recovery-intent.json and application state before operator clearance"
        );
        let active = match std::fs::read(root.join("active.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
            Err(error) => return Err(error.into()),
        };
        let previous_versions = match std::fs::read(root.join("previous.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
            Err(e) => return Err(e.into()),
        };
        let recovered = match std::fs::read(root.join("recovered.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
            Err(e) => return Err(e.into()),
        };
        let current = Instance::current();
        let previous = Instance::recorded(&root)?;
        if let Some(previous) = &previous {
            anyhow::ensure!(
                !previous.still_running(&current),
                "another daemon instance (pid {}) is supervising the components in {}",
                previous.pid,
                root.display()
            );
        }
        let previous_status: BTreeMap<String, Status> = std::fs::read(root.join("status.json"))
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .unwrap_or_default();
        contain_descendants(&root)?;
        // A sealed coalition recorded by the previous instance (macOS) holds
        // all of its descendants, wherever they went.
        let previous_coalition = previous.as_ref().filter(|previous| previous.same_boot(&current)).and_then(|previous| previous.coalition);
        let left = reap_previous_runs(&root, &previous_status, previous_coalition)?;
        if left > 0 {
            warn!(left, "Killed component runs left by a previous daemon instance");
        }
        // The previous instance ended without stopping the components it
        // started. What the recovery above cannot see (a descendant that
        // cleared its environment and lost its parent chain, of a run or of an
        // install that recorded no status) must be ruled out some other way,
        // or nothing may start. Only a record written before its first
        // component process proves there were none.
        let proven_idle = previous.as_ref().is_some_and(|previous| previous.idle) && previous_status.is_empty();
        if let (Some(previous), false) = (&previous, proven_idle) {
            if let Err(reason) = previous.left_nothing(&current, Path::new(CGROUP_ROOT), cgroup_writable) {
                anyhow::bail!(
                    "refusing to start components: the previous daemon instance (pid {}) ended without \
                     stopping them and none of its descendants may still run, but {reason}. Reboot, or \
                     make sure no process it started remains and remove {}",
                    previous.pid,
                    root.join(INSTANCE).display()
                );
            }
            info!(
                previous = previous.pid,
                rebooted = !previous.same_boot(&current),
                "Previous daemon instance ended without stopping its components; none of its processes can remain"
            );
        }
        // Only now that no leftover remains can this instance's coalition be
        // its own.
        let current = Instance { coalition: dedicated_coalition(), ..current };
        set_sweep_coalition(current.coalition);
        if cfg!(target_os = "macos") && current.coalition.is_none() {
            warn!("Not the only process of a sealed launchd coalition: descendants that leave their \
                   process group and clear their environment cannot be contained, and a crash of this \
                   daemon cannot be recovered from without a reboot");
        }
        let record = Record {
            path: root.join(INSTANCE),
            instance: Arc::new(Mutex::new(current)),
        };
        record.save()?;
        let supervisor = Self {
            root,
            settings,
            active,
            running: BTreeMap::new(),
            status: Arc::new(Mutex::new(BTreeMap::new())),
            record,
            recovery_commands: BTreeMap::new(),
            previous: previous_versions,
            recovered,
        };
        write_status(&supervisor.root, &supervisor.status)?;
        Ok(supervisor)
    }

    pub fn configure_recovery(&mut self, commands: BTreeMap<String, String>) {
        self.recovery_commands = commands;
    }

    /// Completed recovery receipts whose committed active specs agree. At
    /// startup no process is running yet; use these only to settle durable
    /// configuration before bootstrap, never as evidence of current health.
    pub fn recoveries_for_start(&self) -> BTreeMap<String,String> {
        self.recovered.iter().filter(|(name,deployment)| self.active.get(*name).is_some_and(|s| &s.deployment == *deployment))
            .map(|(name,deployment)| (name.clone(),deployment.clone())).collect()
    }

    /// Only confirmed, running previous versions can restore the component journal.
    pub fn restored(&self, name: &str, deployment: &str) -> bool {
        self.active.get(name).is_some_and(|s| s.deployment == deployment)
            && self.status.lock().unwrap().get(name).is_some_and(|s| s.deployment == deployment && s.phase == Phase::Running)
    }

    /// Runtime crash/readiness failure after the restart budget is exhausted.
    /// Only explicitly opted-in applications with a retained previous version
    /// can recover; no platform installation or reboot is initiated.
    pub async fn recover_broken(&mut self, runner: &ApplyRunner, confirmed: impl Fn(&str) -> bool) -> anyhow::Result<()> {
        anyhow::ensure!(!self.root.join("recovery-intent.json").exists(),
            "application recovery requires operator inspection");
        let broken: Vec<_> = self.status.lock().unwrap().iter()
            .filter(|(_, status)| status.phase == Phase::Broken)
            .map(|(name, _)| name.clone()).collect();
        for name in broken {
            if !confirmed(&name) || !self.recovery_commands.contains_key(&name) { continue; }
            let Some(previous) = self.previous.get(&name).cloned() else { continue; };
            if self.active.get(&name).is_some_and(|s| s.deployment == previous.deployment) { continue; }
            self.recover_application(&previous, runner, "application-recovery").await?;
            self.start(previous.clone());
            self.wait_ready(&name).await?;
            self.recovered.insert(name.clone(), previous.deployment.clone());
            atomic_write(&self.root.join("recovered.json"), &serde_json::to_vec(&self.recovered)?)?;
            self.active.insert(name.clone(), previous);
            self.save_active()?;
            self.previous.remove(&name);
            atomic_write(&self.root.join("previous.json"), &serde_json::to_vec(&self.previous)?)?;
            self.complete_recovery()?;
            info!(component = name, "Application recovered after runtime failure");
        }
        Ok(())
    }

    pub fn recovered(&self) -> &BTreeMap<String, String> { &self.recovered }

    /// Invalidate a past recovery before admitting any new installer for this
    /// component, including a transition away from supervision.
    pub fn begin_install(&mut self, name: &str) -> anyhow::Result<()> {
        self.recovered.remove(name);
        atomic_write(&self.root.join("recovered.json"), &serde_json::to_vec(&self.recovered)?)
    }


    async fn recover_application(&mut self, previous: &Spec, runner: &ApplyRunner, request: &str) -> anyhow::Result<()> {
        let Some(command) = self.recovery_commands.get(&previous.name).cloned() else {
            return Ok(());
        };
        // Save before inspection or command execution: any failure leaves the
        // application state requiring inspection instead of a silent bootstrap.
        let intent = self.root.join("recovery-intent.json");
        atomic_write(&intent, &serde_json::to_vec(&serde_json::json!({"previous": previous, "command": command}))?)?;
        self.stop(&previous.name).await;
        let workspace = self.workspace(previous);
        let expected = std::fs::read_to_string(workspace.join("artifact.sha256"))?;
        anyhow::ensure!(crate::walrus_client::sha256_file(&workspace.join("artifact"))? == expected,
            "previous application artifact digest mismatch; recovery refused");
        // Like the version's other commands, it may see its configuration,
        // so its output goes to the private component log.
        let materialized = materialize(&self.root, &self.settings.overlay_dir, &previous.name, &workspace)?;
        let output = self.component_log(&previous.name)?;
        self.record.arm()?;
        runner.apply(&workspace.join("artifact"), Some(&command), request, materialized.as_deref(), Some(output)).await
            .map_err(|e| e.context("application recovery failed; operator inspection required"))?;
        Ok(())
    }

    fn complete_recovery(&self) -> anyhow::Result<()> {
        let intent = self.root.join("recovery-intent.json");
        if intent.exists() {
            std::fs::remove_file(intent)?;
            std::fs::File::open(&self.root)?.sync_all()?;
        }
        Ok(())
    }

    /// Start every committed component (daemon start, including after a
    /// reboot), dependencies first: each waits until its supervised
    /// dependencies were started and became ready (or failed to).
    pub async fn bootstrap(&mut self) {
        for spec in start_order(&self.active) {
            self.start(spec.clone());
            match self.wait_ready(&spec.name).await {
                Ok(()) => info!(component = spec.name, version = spec.version, "Component started"),
                Err(error) => warn!(component = spec.name, version = spec.version, %error, "Component did not become ready; its restart policy applies"),
            }
        }
    }

    /// Install `artifact` as a new version, replace the running one and wait
    /// until it is ready. On failure the previous version is restored and the
    /// error says so; the new version is never committed.
    /// `configuration` is the version's public configuration record.
    pub async fn deploy(
        &mut self,
        spec: Spec,
        artifact: &Path,
        install: Option<&str>,
        runner: &ApplyRunner,
        request_id: &str,
        configuration: &configuration::Record,
    ) -> anyhow::Result<()> {
        anyhow::ensure!(!self.root.join("recovery-intent.json").exists(),
            "application recovery requires operator inspection");
        anyhow::ensure!(safe_segment(&spec.name), "component name is not a safe path segment");
        anyhow::ensure!(safe_segment(&spec.version), "component version is not a safe path segment");
        // A prior runtime recovery cannot confirm the outcome of this new
        // deployment. Persist invalidation before any workspace/install work,
        // including paths that return early or are interrupted.
        self.begin_install(&spec.name)?;
        let workspace = self.workspace(&spec);
        let _ = std::fs::remove_dir_all(&workspace);
        // Durable before the install starts: a restart must find the version
        // and the configuration record it was installed with.
        configuration::create_dir_durable(self.root.parent().unwrap_or(&self.root), &workspace)?;
        let installed = workspace.join("artifact");
        std::fs::rename(artifact, &installed)?;
        std::fs::File::open(&installed)?.sync_all()?;
        atomic_write(&workspace.join("artifact.sha256"), crate::walrus_client::sha256_file(&installed)?.as_bytes())?;
        configuration::write_private(&workspace.join(CONFIGURATION_STATE), configuration)?;
        let materialized = materialize(&self.root, &self.settings.overlay_dir, &spec.name, &workspace)?;
        let previous = self.active.get(&spec.name).cloned();
        if let Some(script) = install {
            self.record.arm().map_err(|error| error.context("install not started"))?;
            // The old version keeps running until the new one is installed.
            let output = self.component_log(&spec.name)?;
            if let Err(error) = runner.apply(&installed, Some(script), request_id, materialized.as_deref(), Some(output)).await {
                if error.downcast_ref::<crate::firmware::Indeterminate>().is_some() {
                    // No destructive recovery after an ambiguous installer result.
                    return Err(error);
                }
                let _ = std::fs::remove_dir_all(&workspace);
                if let Some(previous) = &previous {
                    if self.recovery_commands.contains_key(&previous.name) {
                        self.recover_application(previous, runner, request_id).await?;
                        self.start(previous.clone());
                        self.wait_ready(&previous.name).await?;
                        self.complete_recovery()?;
                        return Err(Recovered(format!("{error:#}; previous application {} recovered", previous.version)).into());
                    }
                }
                return Err(error.context("install failed; previous application retained"));
            }
        }
        self.stop(&spec.name).await;
        self.start(spec.clone());
        match self.wait_ready(&spec.name).await {
            Ok(()) => {
                if let Some(previous) = &previous {
                    self.previous.insert(spec.name.clone(), previous.clone());
                } else {
                    self.previous.remove(&spec.name);
                }
                atomic_write(&self.root.join("previous.json"), &serde_json::to_vec(&self.previous)?)?;
                self.recovered.remove(&spec.name);
                atomic_write(&self.root.join("recovered.json"), &serde_json::to_vec(&self.recovered)?)?;
                self.active.insert(spec.name.clone(), spec.clone());
                self.save_active()?;
                self.collect_versions(&spec, previous.as_ref());
                info!(component = spec.name, version = spec.version, "Component version committed");
                Ok(())
            }
            Err(error) => {
                self.stop(&spec.name).await;
                let _ = std::fs::remove_dir_all(&workspace);
                let Some(previous) = previous else {
                    return Err(error);
                };
                self.recover_application(&previous, runner, request_id).await?;
                self.start(previous.clone());
                let restored = match self.wait_ready(&previous.name).await {
                    Ok(()) => { self.complete_recovery()?; "ready".to_string() },
                    Err(error) => format!("not ready: {error:#}"),
                };
                let message = format!("{error:#}; rolled back to {} ({restored})", previous.version);
                if restored == "ready" && (install.is_none() || self.recovery_commands.contains_key(&previous.name)) {
                    return Err(Recovered(message).into());
                }
                anyhow::bail!(message)
            }
        }
    }

    /// Run a version's validate command against a candidate configuration
    /// file, before anything is installed: `Ok(true)` accepts it, `Ok(false)`
    /// defers it (exit 75), and any other end rejects it. It runs like the
    /// component's other commands, in its work directory, with its output in
    /// the component log rather than the daemon's.
    pub async fn validate(
        &self,
        name: &str,
        version: &str,
        command: &str,
        artifact: &Path,
        configuration: &Path,
        timeout: Duration,
    ) -> anyhow::Result<bool> {
        anyhow::ensure!(safe_segment(name), "component name is not a safe path segment");
        let component = self.root.join(name);
        let work = component.join("work");
        std::fs::create_dir_all(&work)?;
        let script = command
            .replace("{artifact}", &quote(artifact))
            .replace("{artifacts}", &quote(artifact.parent().unwrap_or(Path::new("/"))))
            .replace("{work}", &quote(&work))
            .replace("{configuration}", &quote(configuration));
        let mut command = tokio::process::Command::new("/bin/sh");
        command.arg("-c").arg(script);
        apply::harden(&mut command, &work, self.settings.max_memory_bytes);
        let log = self.component_log(name)?;
        command
            .env(MARKER, marker(&self.root))
            .env("SUIFLEET_COMPONENT", name)
            .env("SUIFLEET_COMPONENT_VERSION", version)
            .env("SUIFLEET_CONFIGURATION", configuration)
            .stdout(log.try_clone()?)
            .stderr(log);
        self.record.arm().map_err(|error| error.context("validate not started"))?;
        let status = apply::run_status(command, timeout)
            .await
            .map_err(|error| error.context("configuration validate command"))?;
        if status.code() == Some(configuration::DEFER_EXIT) {
            return Ok(false);
        }
        apply::exit_result(status).map_err(|error| error.context("configuration rejected by the component's validate command"))?;
        Ok(true)
    }

    /// `name`'s component log (0600), which receives the output of all its
    /// commands, install included: they see its materialized configuration,
    /// so their output must stay out of the daemon's diagnostics.
    pub fn component_log(&self, name: &str) -> anyhow::Result<std::fs::File> {
        anyhow::ensure!(safe_segment(name), "component name is not a safe path segment");
        let component = self.root.join(name);
        std::fs::create_dir_all(&component)?;
        Ok(open_log(&component.join("component.log"))?)
    }

    /// The deployment of `name`'s committed version, if it is supervised.
    pub fn committed(&self, name: &str) -> Option<&str> {
        self.active.get(name).map(|spec| spec.deployment.as_str())
    }

    /// The public configuration record `name`'s committed version was
    /// deployed with; none for a version predating configuration management.
    pub fn configuration(&self, name: &str) -> anyhow::Result<Option<configuration::Record>> {
        let Some(spec) = self.active.get(name) else { return Ok(None) };
        let record = match std::fs::read(self.workspace(spec).join(CONFIGURATION_STATE)) {
            Ok(bytes) => serde_json::from_slice::<configuration::Record>(&bytes)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        anyhow::ensure!(record.deployment == spec.deployment, "configuration record of {name} belongs to another deployment");
        Ok(Some(record))
    }

    /// Whether `name` is a committed or running supervised component.
    pub fn supervises(&self, name: &str) -> bool {
        self.active.contains_key(name) || self.running.contains_key(name)
    }

    /// End supervision of `name` (replaced by a version without a run
    /// command): stop it with its descendants and uncommit it, so no later
    /// daemon start brings it back. Its work directory and log are kept.
    pub async fn retire(&mut self, name: &str) -> anyhow::Result<()> {
        self.stop(name).await;
        if self.active.remove(name).is_some() {
            self.save_active()?;
        }
        self.previous.remove(name);
        self.recovered.remove(name);
        atomic_write(&self.root.join("previous.json"), &serde_json::to_vec(&self.previous)?)?;
        atomic_write(&self.root.join("recovered.json"), &serde_json::to_vec(&self.recovered)?)?;
        self.status.lock().unwrap().remove(name);
        write_status(&self.root, &self.status)?;
        if safe_segment(name) {
            let _ = std::fs::remove_dir_all(self.root.join(name).join("versions"));
        }
        info!(component = name, "Component no longer supervised");
        Ok(())
    }

    /// Stop every component (daemon shutdown), dependents before their
    /// dependencies.
    pub async fn stop_all(&mut self) {
        let mut names: Vec<String> = start_order(&self.active).into_iter().rev().map(|spec| spec.name).collect();
        names.extend(self.running.keys().filter(|name| !self.active.contains_key(*name)).cloned());
        for name in names {
            self.stop(&name).await;
        }
        // The sweeper thread ends with the daemon: collect descendants that
        // were orphaned by these stops now.
        for _ in 0..3 {
            sweep_orphans();
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        // Everything stopped: the next daemon start needs no proof of that.
        if let Err(error) = std::fs::remove_file(self.root.join(INSTANCE)) {
            if error.kind() != std::io::ErrorKind::NotFound {
                warn!(%error, "Cannot clear the daemon instance record");
            }
        }
    }

    pub fn status(&self) -> BTreeMap<String, Status> {
        self.status.lock().unwrap().clone()
    }

    /// Local control retains the committed version. Stops last until an explicit
    /// start/restart, deployment replacement or daemon restart.
    pub async fn local_control(&mut self, name: &str, operation: &str) -> anyhow::Result<()> {
        let spec = self.active.get(name).cloned()
            .ok_or_else(|| anyhow::anyhow!("component is not supervised"))?;
        if operation == "stop" || operation == "restart" {
            anyhow::ensure!(!self.active.values().any(|s| s.name != name &&
                s.after.iter().any(|d| d == name) && self.running.contains_key(&s.name)),
                "stop dependents first");
            self.stop(name).await;
            // An already-finished Broken/Exited task cannot observe the stop
            // signal. Record explicit intent so the next poll does not recover
            // and restart a component the operator just stopped.
            if let Some(status) = self.status.lock().unwrap().get_mut(name) {
                status.phase = Phase::Stopped;
                status.pid = None;
                status.pid_start = None;
                status.since = SystemTime::now().duration_since(UNIX_EPOCH)
                    .map(|d| d.as_secs()).unwrap_or(0);
            }
            write_status(&self.root, &self.status)?;
        }
        if operation == "start" || operation == "restart" {
            let status = self.status();
            anyhow::ensure!(spec.after.iter().all(|d| status.get(d)
                .is_some_and(|s| s.phase == Phase::Running)), "dependency is not ready");
            if self.running.get(name).is_some_and(|h| h.task.is_finished()) {
                self.stop(name).await;
            }
            if !self.running.contains_key(name) { self.start(spec); }
            self.wait_ready(name).await?;
        }
        Ok(())
    }

    pub fn local_logs(&self, name: &str) -> anyhow::Result<String> {
        use std::io::{Read, Seek, SeekFrom};
        anyhow::ensure!(safe_segment(name) && self.active.contains_key(name), "unknown component");
        let mut file = std::fs::OpenOptions::new().read(true)
            .custom_flags(libc::O_NOFOLLOW).open(self.root.join(name).join("component.log"))?;
        let size = file.metadata()?.len();
        file.seek(SeekFrom::Start(size.saturating_sub(8 * 1024)))?;
        let mut bytes = Vec::new();
        // Worst-case JSON escaping costs six bytes per input byte.
        file.take(8 * 1024).read_to_end(&mut bytes)?;
        Ok(String::from_utf8_lossy(&bytes).into_owned())
    }

    fn workspace(&self, spec: &Spec) -> PathBuf {
        let tag = hex::encode(&crate::identity::hash(spec.deployment.as_bytes())[..4]);
        self.root
            .join(&spec.name)
            .join("versions")
            .join(format!("{}-{tag}", spec.version))
    }

    fn start(&mut self, spec: Spec) {
        let (stop, stop_rx) = watch::channel(false);
        let (phase_tx, phase) = watch::channel(Phase::Starting);
        let context = Context {
            component: self.root.join(&spec.name),
            workspace: self.workspace(&spec),
            root: self.root.clone(),
            settings: self.settings.clone(),
            status: self.status.clone(),
            record: self.record.clone(),
            phase: phase_tx,
            spec: spec.clone(),
        };
        let task = tokio::spawn(supervise(context, stop_rx));
        self.running.insert(spec.name, Handle { stop, phase, task });
    }

    async fn stop(&mut self, name: &str) {
        if let Some(handle) = self.running.remove(name) {
            let _ = handle.stop.send(true);
            let _ = handle.task.await;
        }
    }

    /// Ready once the run reaches Running; any earlier end is a failure.
    async fn wait_ready(&mut self, name: &str) -> anyhow::Result<()> {
        let handle = self
            .running
            .get_mut(name)
            .ok_or_else(|| anyhow::anyhow!("component {name} is not running"))?;
        let limit = self.settings.ready_timeout + self.settings.startup + Duration::from_secs(5);
        let phase = tokio::time::timeout(
            limit,
            handle.phase.wait_for(|phase| *phase != Phase::Starting),
        )
        .await
        .map_err(|_| anyhow::anyhow!("component {name} did not become ready in time"))?
        .map(|phase| *phase)
        .unwrap_or(Phase::Exited);
        if phase == Phase::Running {
            return Ok(());
        }
        let last = self
            .status
            .lock()
            .unwrap()
            .get(name)
            .and_then(|status| status.last_exit.clone())
            .unwrap_or_else(|| format!("{phase:?}"));
        anyhow::bail!("component {name} failed to start: {last}")
    }

    fn save_active(&self) -> anyhow::Result<()> {
        atomic_write(&self.root.join("active.json"), &serde_json::to_vec_pretty(&self.active)?)
    }

    /// Keep the committed and previous versions; remove the rest.
    fn collect_versions(&self, current: &Spec, previous: Option<&Spec>) {
        let keep: BTreeSet<PathBuf> = [Some(current), previous]
            .into_iter()
            .flatten()
            .map(|spec| self.workspace(spec))
            .collect();
        let Ok(entries) = std::fs::read_dir(self.root.join(&current.name).join("versions")) else {
            return;
        };
        for entry in entries.flatten() {
            if !keep.contains(&entry.path()) {
                let _ = std::fs::remove_dir_all(entry.path());
            }
        }
    }
}

/// Committed specs ordered so dependencies come before their dependents
/// (names break ties; a cycle, which the resolver rejects, falls back to name
/// order rather than skipping anything).
fn start_order(active: &BTreeMap<String, Spec>) -> Vec<Spec> {
    let mut order: Vec<Spec> = Vec::new();
    let mut placed = BTreeSet::new();
    while order.len() < active.len() {
        let ready = active.values().find(|spec| {
            !placed.contains(&spec.name)
                && spec.after.iter().all(|dep| placed.contains(dep) || !active.contains_key(dep))
        });
        let next = ready
            .or_else(|| active.values().find(|spec| !placed.contains(&spec.name)))
            .unwrap()
            .clone();
        placed.insert(next.name.clone());
        order.push(next);
    }
    order
}

struct Context {
    spec: Spec,
    component: PathBuf,
    workspace: PathBuf,
    root: PathBuf,
    settings: Settings,
    status: Arc<Mutex<BTreeMap<String, Status>>>,
    record: Record,
    phase: watch::Sender<Phase>,
}
impl Context {
    fn set(&self, phase: Phase, pid: Option<u32>, restarts: u32, last_exit: Option<String>) {
        {
            let mut status = self.status.lock().unwrap();
            let entry = status.entry(self.spec.name.clone()).or_insert_with(|| Status {
                version: String::new(),
                deployment: String::new(),
                phase,
                pid: None,
                pid_start: None,
                restarts: 0,
                last_exit: None,
                since: 0,
            });
            entry.version = self.spec.version.clone();
            entry.deployment = self.spec.deployment.clone();
            entry.phase = phase;
            entry.pid = pid;
            entry.pid_start = pid.and_then(start_time);
            entry.restarts = restarts;
            if last_exit.is_some() {
                entry.last_exit = last_exit;
            }
            entry.since = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
        }
        if let Err(error) = write_status(&self.root, &self.status) {
            warn!(%error, "Cannot write component status");
        }
        self.phase.send_replace(phase);
    }

    fn command(&self, template: &str, pid: Option<u32>) -> tokio::process::Command {
        let work = self.component.join("work");
        let configuration = self.workspace.join(CONFIGURATION);
        let mut script = template
            .replace("{artifact}", &quote(&self.workspace.join("artifact")))
            .replace("{artifacts}", &quote(&self.workspace))
            .replace("{work}", &quote(&work))
            .replace("{configuration}", &quote(&configuration));
        if let Some(pid) = pid {
            script = script.replace("{pid}", &pid.to_string());
        }
        let mut command = tokio::process::Command::new("/bin/sh");
        command.arg("-c").arg(script);
        apply::harden(&mut command, &work, self.settings.max_memory_bytes);
        command
            .env(MARKER, marker(&self.root))
            .env("SUIFLEET_COMPONENT", &self.spec.name)
            .env("SUIFLEET_COMPONENT_VERSION", &self.spec.version);
        // Versions deployed before configuration management have none.
        if self.workspace.join(CONFIGURATION_STATE).exists() {
            command.env("SUIFLEET_CONFIGURATION", &configuration);
        }
        command
    }

    /// A bounded helper command (readiness check or shutdown).
    /// Hardened once, by [`Context::command`], so it keeps the component's
    /// environment and recovery marker. It sees the materialized
    /// configuration, so its output goes to the private component log like
    /// every other lifecycle command, never to the daemon's diagnostics.
    async fn helper(&self, template: &str, pid: u32, timeout: Duration) -> anyhow::Result<()> {
        let log = open_log(&self.component.join("component.log"))?;
        let mut command = self.command(template, Some(pid));
        command.stdout(log.try_clone()?).stderr(log);
        apply::run_hardened(command, timeout).await
    }

    async fn ready(&self, pid: u32) -> Result<(), String> {
        let Some(check) = &self.spec.readiness else {
            tokio::time::sleep(self.settings.startup).await;
            return Ok(());
        };
        let deadline = Instant::now() + self.settings.ready_timeout;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            let error = match self.helper(check, pid, remaining.min(READINESS_ATTEMPT).max(Duration::from_secs(1))).await {
                Ok(()) => return Ok(()),
                Err(error) => error,
            };
            if Instant::now() >= deadline {
                return Err(format!(
                    "readiness check did not pass within {}s: {error:#}",
                    self.settings.ready_timeout.as_secs()
                ));
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    }

    fn spawn(&self) -> anyhow::Result<Tracked> {
        let work = self.component.join("work");
        std::fs::create_dir_all(&work)?;
        // Every start reads the overlay again: a provisioned or rotated
        // device-local value applies on the next restart.
        materialize(&self.root, &self.settings.overlay_dir, &self.spec.name, &self.workspace)?;
        self.record.arm()?;
        let log = open_log(&self.component.join("component.log"))?;
        let mut command = self.command(&self.spec.run, None);
        command.stdout(log.try_clone()?).stderr(log);
        Ok(spawn_tracked(&mut command)?)
    }

    /// Graceful stop: the shutdown command, then SIGTERM, then SIGKILL, always
    /// ending with the whole process group killed and the run reaped.
    async fn terminate(&self, child: &mut Tracked) {
        let pid = child.pid();
        let wait = self.settings.stop_timeout;
        if let Some(shutdown) = &self.spec.shutdown {
            if let Err(error) = self.helper(shutdown, pid, wait).await {
                warn!(component = self.spec.name, %error, "Shutdown command failed");
            }
        } else {
            signal_group(pid, libc::SIGTERM);
        }
        if tokio::time::timeout(wait, child.wait()).await.is_err() {
            signal_group(pid, libc::SIGTERM);
            let _ = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
        }
        apply::kill_group(pid as i32);
        let _ = child.wait().await;
        // Descendants that left the group were orphaned into this process by
        // the run's exit: remove them now, not on the sweeper's next pass.
        sweep_orphans();
    }
}

enum End {
    Exit(std::io::Result<ExitStatus>),
    NotReady(String),
    Stop,
}

async fn supervise(context: Context, mut stop: watch::Receiver<bool>) {
    let name = context.spec.name.clone();
    let mut restarts = 0u32;
    let mut recent: VecDeque<Instant> = VecDeque::new();
    let mut backoff = Duration::from_secs(1);
    loop {
        context.set(Phase::Starting, None, restarts, None);
        let mut child = match context.spawn() {
            Ok(child) => child,
            Err(error) => {
                context.set(Phase::Broken, None, restarts, Some(format!("cannot start: {error:#}")));
                return;
            }
        };
        let pid = child.pid();
        context.set(Phase::Starting, Some(pid), restarts, None);
        let started = Instant::now();
        let end = async {
            tokio::select! {
                ready = context.ready(pid) => {
                    if let Err(reason) = ready {
                        return End::NotReady(reason);
                    }
                }
                status = child.wait() => return End::Exit(status),
                _ = stop.wait_for(|stop| *stop) => return End::Stop,
            }
            context.set(Phase::Running, Some(pid), restarts, None);
            tokio::select! {
                status = child.wait() => End::Exit(status),
                _ = stop.wait_for(|stop| *stop) => End::Stop,
            }
        }
        .await;
        let (failed, reason) = match end {
            End::Stop => {
                context.set(Phase::Stopping, Some(pid), restarts, None);
                context.terminate(&mut child).await;
                context.set(Phase::Stopped, None, restarts, Some("stopped by the supervisor".into()));
                return;
            }
            End::NotReady(reason) => (true, reason),
            End::Exit(status) => match status {
                Ok(status) => (!status.success(), describe(status)),
                Err(error) => (true, format!("wait failed: {error}")),
            },
        };
        // Whatever the run left behind goes with it, in or out of its group.
        apply::kill_group(pid as i32);
        let _ = child.wait().await;
        sweep_orphans();
        warn!(component = name, %reason, "Component run ended");
        let restart = match context.spec.restart {
            Restart::Never => false,
            Restart::OnFailure => failed,
            Restart::Always => true,
        };
        if !restart {
            context.set(Phase::Exited, None, restarts, Some(reason));
            return;
        }
        if started.elapsed() >= STABLE_RUN {
            backoff = Duration::from_secs(1);
        }
        let now = Instant::now();
        recent.push_back(now);
        while recent.front().is_some_and(|at| now.duration_since(*at) > RESTART_WINDOW) {
            recent.pop_front();
        }
        if recent.len() as u32 > context.settings.max_restarts {
            context.set(Phase::Broken, None, restarts, Some(format!("{reason}; restart budget exhausted")));
            return;
        }
        context.set(Phase::Backoff, None, restarts, Some(reason));
        tokio::select! {
            _ = tokio::time::sleep(backoff) => {}
            _ = stop.wait_for(|stop| *stop) => {
                context.set(Phase::Stopped, None, restarts, None);
                return;
            }
        }
        backoff = (backoff * 2).min(MAX_BACKOFF);
        restarts += 1;
    }
}

fn describe(status: ExitStatus) -> String {
    use std::os::unix::process::ExitStatusExt;
    match (status.code(), status.signal()) {
        (Some(code), _) => format!("exited with code {code}"),
        (None, Some(signal)) => format!("killed by signal {signal}"),
        _ => "terminated abnormally".into(),
    }
}

fn signal_group(pid: u32, signal: i32) {
    unsafe {
        libc::kill(-(pid as i32), signal);
    }
}

/// Write a workspace's `configuration.json` from its public record and the
/// device overlay; returns its path, or nothing for a version deployed
/// without configuration management. The overlay's values are recorded for
/// log redaction first (`root` is `<state_dir>/supervised`).
fn materialize(root: &Path, overlay_dir: &Path, name: &str, workspace: &Path) -> anyhow::Result<Option<PathBuf>> {
    let Some(record) = std::fs::read(workspace.join(CONFIGURATION_STATE))
        .ok()
        .map(|bytes| serde_json::from_slice::<configuration::Record>(&bytes))
        .transpose()?
    else {
        return Ok(None);
    };
    let overlay = configuration::overlay(overlay_dir, name)?;
    crate::logs::remember(root.parent().unwrap_or(root), &overlay)?;
    let value = configuration::materialize(&record, &overlay)?;
    let path = workspace.join(CONFIGURATION);
    configuration::write_private(&path, &value)?;
    Ok(Some(path))
}

fn quote(path: &Path) -> String {
    shell_words::quote(&path.to_string_lossy()).into_owned()
}

fn safe_segment(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value != "."
        && value != ".."
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.+".contains(&b))
}

fn open_log(path: &Path) -> std::io::Result<std::fs::File> {
    if std::fs::metadata(path).is_ok_and(|meta| meta.len() > LOG_ROTATE_BYTES) {
        let _ = std::fs::rename(path, path.with_extension("log.1"));
    }
    let file = std::fs::OpenOptions::new().create(true).append(true).mode(0o600).open(path)?;
    // Also tightens a log created before it held configured output. With
    // `[runtime] log_group_read`, the daemon's group may read it (SF-060).
    file.set_permissions(std::fs::Permissions::from_mode(crate::logs::private_mode()))?;
    if let Some(component) = path.parent() {
        crate::logs::share_dir(component)?;
        if let Some(root) = component.parent() {
            crate::logs::share_dir(root)?;
        }
    }
    Ok(file)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, bytes)?;
    std::fs::File::open(&temporary)?.sync_all()?;
    std::fs::rename(&temporary, path)?;
    std::fs::File::open(path.parent().unwrap())?.sync_all()?;
    Ok(())
}

fn write_status(root: &Path, status: &Mutex<BTreeMap<String, Status>>) -> anyhow::Result<()> {
    // Hold the lock until the rename: concurrent writers share the temporary
    // file, and the last state recorded must be the last one on disk (a
    // restarted daemon reaps leftover runs by the PIDs recorded there).
    let status = status.lock().unwrap();
    let bytes = serde_json::to_vec_pretty(&*status)?;
    let temporary = root.join("status.json.tmp");
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(&temporary, root.join("status.json"))?;
    Ok(())
}

// --- Descendant containment -------------------------------------------------

/// Environment variable naming the supervisor that started a process. Every
/// component process inherits it (across setsid too), so a later daemon can
/// find runs left behind even after they were reparented away from it.
const MARKER: &str = "SUIFLEET_SUPERVISOR";

/// This state root's marker value.
fn marker(root: &Path) -> String {
    hex::encode(&crate::identity::hash(root.as_os_str().as_encoded_bytes())[..16])
}

fn carries(pid: u32, marker: &str) -> bool {
    crate::platform::carries(pid, format!("{MARKER}={marker}").as_bytes())
}

/// A daemon that died without stopping its components (crash, SIGKILL)
/// leaves their runs behind, and descendants reparented away from it. Before
/// anything starts again, find every leftover: the recorded runs, their
/// process groups, every process carrying this root's marker (including ones
/// that left the group with setsid and lost their parent) and all of their
/// descendants (including ones that cleared their environment). They are
/// frozen with SIGSTOP until the set is closed, so none can fork away, then
/// killed. Fails if any survives: no component may run twice.
fn reap_previous_runs(root: &Path, previous: &BTreeMap<String, Status>, coalition: Option<u64>) -> anyhow::Result<usize> {
    let me = std::process::id();
    let marker = marker(root);
    let mut runs = BTreeSet::new();
    let mut groups = BTreeSet::new();
    for status in previous.values() {
        let (Some(pid), Some(started)) = (status.pid, status.pid_start) else {
            continue;
        };
        // Either the recorded run itself, or its group outliving it (Linux
        // does not reuse a PID while it is still a process-group ID); a
        // different process under a reused PID is left alone.
        let current = start_time(pid);
        if current == Some(started) {
            runs.insert(pid);
        }
        if current == Some(started) || current.is_none() {
            groups.insert(pid);
        }
    }
    let leftovers = |table: &[Proc]| -> BTreeSet<u32> {
        let mut found: BTreeSet<u32> = table
            .iter()
            .filter(|p| p.pid != me)
            .filter(|p| runs.contains(&p.pid) || groups.contains(&p.pgrp) || carries(p.pid, &marker) || in_coalition(p.pid, coalition))
            .map(|p| p.pid)
            .collect();
        loop {
            let more: Vec<u32> = table
                .iter()
                .filter(|p| p.pid != me && !found.contains(&p.pid) && found.contains(&p.ppid))
                .map(|p| p.pid)
                .collect();
            if more.is_empty() {
                return found;
            }
            found.extend(more);
        }
    };
    let deadline = Instant::now() + Duration::from_secs(5);
    // Each frozen process with its start time, so a PID reused once it died
    // is never signalled.
    let mut frozen: BTreeMap<u32, Option<u64>> = BTreeMap::new();
    loop {
        let found = leftovers(&processes());
        let new: Vec<u32> = found.into_iter().filter(|pid| !frozen.contains_key(pid)).collect();
        if new.is_empty() {
            break;
        }
        for pid in new {
            let started = start_time(pid);
            unsafe {
                libc::kill(pid as i32, libc::SIGSTOP);
            }
            frozen.insert(pid, started);
        }
        anyhow::ensure!(Instant::now() < deadline, "cannot freeze component runs left by a previous daemon");
    }
    let killed = frozen.len();
    // SIGKILL is delivered asynchronously: wait until every leftover is really
    // dead (gone or a zombie), or a restart could overlap them.
    loop {
        frozen.retain(|pid, started| start_time(*pid).is_some() && start_time(*pid) == *started);
        for &pid in frozen.keys() {
            unsafe {
                libc::kill(pid as i32, libc::SIGKILL);
            }
        }
        let late = leftovers(&processes());
        if frozen.is_empty() && late.is_empty() {
            return Ok(killed);
        }
        if Instant::now() >= deadline {
            let alive: BTreeSet<u32> = frozen.keys().copied().chain(late).collect();
            anyhow::bail!("component processes left by a previous daemon survived SIGKILL: {alive:?}");
        }
        for pid in late {
            frozen.entry(pid).or_insert_with(|| start_time(pid));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// File (under the state root) recording the daemon instance that supervises
/// components, from its start until it stopped all of them.
const INSTANCE: &str = "instance.json";
const CGROUP_ROOT: &str = "/sys/fs/cgroup";

/// A daemon instance, as recorded in `instance.json`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
struct Instance {
    pid: u32,
    pid_start: Option<u64>,
    /// Kernel boot ID, when readable: a later boot ended every process.
    boot_id: Option<String>,
    /// Its cgroup v2 path, when known.
    cgroup: Option<String>,
    /// Its resource coalition (macOS), recorded only when it was the sole
    /// member and could not leave it: every process it started is in it.
    #[serde(default)]
    coalition: Option<u64>,
    /// Recorded before it started any component process: none can remain.
    /// Missing (or an unreadable record) proves nothing.
    #[serde(default)]
    idle: bool,
}
impl Instance {
    fn current() -> Self {
        let pid = std::process::id();
        Self {
            pid,
            pid_start: start_time(pid),
            boot_id: crate::platform::boot_id(),
            cgroup: std::fs::read_to_string("/proc/self/cgroup").ok().and_then(|text| {
                text.lines()
                    .find_map(|line| line.strip_prefix("0::"))
                    .map(str::to_owned)
            }),
            coalition: None,
            idle: true,
        }
    }

    /// The record left by an instance that did not stop its components. An
    /// unreadable one proves nothing, so it counts as such an instance.
    fn recorded(root: &Path) -> anyhow::Result<Option<Self>> {
        match std::fs::read(root.join(INSTANCE)) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes).unwrap_or_default())),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    fn same_boot(&self, current: &Self) -> bool {
        match (&self.boot_id, &current.boot_id) {
            (Some(then), Some(now)) => then == now,
            _ => true,
        }
    }

    /// Another live daemon (not this process reopening the state root).
    fn still_running(&self, current: &Self) -> bool {
        self.pid != current.pid
            && self.pid_start.is_some()
            && self.same_boot(current)
            && start_time(self.pid) == self.pid_start
    }

    /// Ok when no process this instance started can still run: the system
    /// rebooted since, or its cgroup (inherited by every descendant) is one
    /// none of them can leave and holds no process but this one.
    fn left_nothing(&self, current: &Self, cgroups: &Path, writable: impl Fn(&Path) -> bool) -> Result<(), String> {
        if !self.same_boot(current) {
            return Ok(());
        }
        #[cfg(target_os = "macos")]
        {
            let _ = (cgroups, writable);
            self.coalition_left_nothing(current, crate::platform::coalition_members)
        }
        #[cfg(not(target_os = "macos"))]
        self.cgroup_left_nothing(current, cgroups, writable)
    }

    /// The macOS equivalent of a sealed cgroup: the sealed coalition it
    /// recorded holds no process but this one.
    #[cfg_attr(not(target_os = "macos"), allow(dead_code))]
    fn coalition_left_nothing(&self, current: &Self, members: impl Fn(u64) -> Vec<u32>) -> Result<(), String> {
        let Some(id) = self.coalition else {
            return Err("it recorded no sealed coalition of its own".into());
        };
        let others = members(id).into_iter().filter(|&pid| pid != current.pid).count();
        if others == 0 {
            Ok(())
        } else {
            Err(format!("its coalition {id} still holds {others} other process(es)"))
        }
    }

    #[cfg_attr(target_os = "macos", allow(dead_code))]
    fn cgroup_left_nothing(&self, current: &Self, cgroups: &Path, writable: impl Fn(&Path) -> bool) -> Result<(), String> {
        let Some(path) = &self.cgroup else {
            return Err("its cgroup is unknown".into());
        };
        let relative = path.trim_start_matches('/');
        if relative.split('/').any(|part| part == "..") {
            return Err(format!("its cgroup {path:?} is not a cgroup path"));
        }
        let dir = cgroups.join(relative);
        // Moving a process out needs write access to the common ancestor's
        // cgroup.procs, which is this cgroup or one of its ancestors.
        for ancestor in dir.ancestors().take_while(|a| a.starts_with(cgroups)) {
            let procs = ancestor.join("cgroup.procs");
            if procs.exists() && writable(&procs) {
                return Err(format!("its processes could leave its cgroup ({} is writable)", procs.display()));
            }
        }
        let mut members = BTreeSet::new();
        let mut pending = vec![dir.clone()];
        while let Some(next) = pending.pop() {
            let procs = match std::fs::read_to_string(next.join("cgroup.procs")) {
                Ok(procs) => procs,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound && next == dir => break,
                Err(error) => return Err(format!("{} cannot be read ({error})", next.join("cgroup.procs").display())),
            };
            members.extend(procs.lines().filter_map(|pid| pid.trim().parse::<u32>().ok()));
            let entries = std::fs::read_dir(&next).map_err(|error| format!("{} cannot be read ({error})", next.display()))?;
            pending.extend(entries.flatten().map(|entry| entry.path()).filter(|path| path.is_dir()));
        }
        members.remove(&current.pid);
        if members.is_empty() {
            Ok(())
        } else {
            Err(format!("its cgroup {path} still holds {} other process(es)", members.len()))
        }
    }
}

/// This daemon's `instance.json`, shared by everything that starts component
/// processes.
#[derive(Clone)]
struct Record {
    path: PathBuf,
    instance: Arc<Mutex<Instance>>,
}
impl Record {
    fn save(&self) -> anyhow::Result<()> {
        let instance = self.instance.lock().unwrap();
        atomic_write(&self.path, &serde_json::to_vec_pretty(&*instance)?)
    }

    /// Mark the record before the first component process starts (on disk
    /// first, so a crash right after leaves the marked record behind).
    fn arm(&self) -> anyhow::Result<()> {
        let mut instance = self.instance.lock().unwrap();
        if !instance.idle && self.path.exists() {
            return Ok(());
        }
        let marked = Instance { idle: false, ..instance.clone() };
        atomic_write(&self.path, &serde_json::to_vec_pretty(&marked)?)
            .map_err(|error| error.context("cannot record that component processes start"))?;
        *instance = marked;
        Ok(())
    }
}

/// Whether this process may write a cgroup file (so move processes with it).
fn cgroup_writable(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(path) = std::ffi::CString::new(path.as_os_str().as_bytes()) else {
        return true;
    };
    // SAFETY: a valid NUL-terminated path.
    unsafe { libc::access(path.as_ptr(), libc::W_OK) == 0 }
}

/// PIDs of children this process spawned and has not reaped yet. Anything
/// else whose parent is this process was orphaned into it (subreaper).
static CHILDREN: Mutex<BTreeSet<u32>> = Mutex::new(BTreeSet::new());

/// A spawned child registered as known; reap it with [`Tracked::wait`].
pub struct Tracked {
    child: tokio::process::Child,
    pid: u32,
}
impl Tracked {
    pub fn pid(&self) -> u32 {
        self.pid
    }
    pub async fn wait(&mut self) -> std::io::Result<ExitStatus> {
        let status = self.child.wait().await;
        if status.is_ok() {
            CHILDREN.lock().unwrap().remove(&self.pid);
        }
        status
    }
}
impl Drop for Tracked {
    fn drop(&mut self) {
        // Reaped (or about to be, by tokio): no longer one of ours.
        if matches!(self.child.try_wait(), Ok(Some(_))) {
            CHILDREN.lock().unwrap().remove(&self.pid);
        }
    }
}

/// Spawn and register atomically with respect to [`sweep_orphans`].
pub fn spawn_tracked(command: &mut tokio::process::Command) -> std::io::Result<Tracked> {
    let mut children = CHILDREN.lock().unwrap();
    let child = command.spawn()?;
    let pid = child
        .id()
        .ok_or_else(|| std::io::Error::other("spawned child has no pid"))?;
    children.insert(pid);
    Ok(Tracked { child, pid })
}

/// Tests elsewhere: spawn a plain child registered as known, so the orphan
/// sweep of a concurrently running supervisor test leaves it alone. Pass the
/// PID to [`forget_std_tracked`] once reaped.
#[cfg(test)]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn spawn_std_tracked(command: &mut std::process::Command) -> std::io::Result<std::process::Child> {
    let mut children = CHILDREN.lock().unwrap();
    let child = command.spawn()?;
    children.insert(child.id());
    Ok(child)
}
#[cfg(test)]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn forget_std_tracked(pid: u32) {
    CHILDREN.lock().unwrap().remove(&pid);
}

/// Become a child subreaper (Linux) and kill orphans as they arrive.
fn contain_descendants(root: &Path) -> anyhow::Result<()> {
    #[cfg(target_os = "linux")]
    {
        let _ = root;
        // SAFETY: plain prctl on this process.
        if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    #[cfg(target_os = "macos")]
    MARKERS.lock().unwrap().insert(marker(root));
    static STARTED: std::sync::Once = std::sync::Once::new();
    // A plain thread: independent of any one async runtime.
    STARTED.call_once(|| {
        std::thread::spawn(|| loop {
            let killed = sweep_orphans();
            if killed > 0 {
                warn!(killed, "Killed unmanaged descendants orphaned into the supervisor");
            }
            std::thread::sleep(Duration::from_secs(1));
        });
    });
    Ok(())
}

/// Kill and reap every process orphaned into this one (a descendant that left
/// its component's process group and outlived its parent). Returns how many
/// live ones were killed.
#[cfg(target_os = "linux")]
pub fn sweep_orphans() -> usize {
    let me = std::process::id();
    let known = CHILDREN.lock().unwrap();
    let mut killed = 0;
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return 0;
    };
    for entry in entries.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(|n| n.parse::<u32>().ok()) else {
            continue;
        };
        if pid == me || known.contains(&pid) {
            continue;
        }
        let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) else {
            continue;
        };
        // Fields after the parenthesised command: state ppid ...
        let Some(rest) = stat.rsplit_once(')').map(|(_, rest)| rest) else {
            continue;
        };
        let mut fields = rest.split_whitespace();
        let state = fields.next().unwrap_or("");
        if fields.next().and_then(|p| p.parse::<u32>().ok()) != Some(me) {
            continue;
        }
        unsafe {
            if state != "Z" {
                libc::kill(pid as i32, libc::SIGKILL);
                killed += 1;
            }
            libc::waitpid(pid as i32, std::ptr::null_mut(), libc::WNOHANG);
        }
    }
    killed
}

/// Markers of the state roots this process supervises (macOS orphan sweep).
#[cfg(target_os = "macos")]
static MARKERS: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());
/// This daemon's sealed coalition, when it has one (macOS orphan sweep).
#[cfg(target_os = "macos")]
static COALITION: Mutex<Option<u64>> = Mutex::new(None);

fn set_sweep_coalition(coalition: Option<u64>) {
    #[cfg(target_os = "macos")]
    {
        *COALITION.lock().unwrap() = coalition;
    }
    #[cfg(not(target_os = "macos"))]
    let _ = coalition;
}

/// Whether a process belongs to the given coalition (macOS only).
fn in_coalition(pid: u32, coalition: Option<u64>) -> bool {
    #[cfg(target_os = "macos")]
    {
        coalition.is_some() && crate::platform::coalition(pid) == coalition
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (pid, coalition);
        false
    }
}

/// This process's coalition, when it is a sealed one of its own (macOS): it
/// is the only member, so a launchd job's, and it is not root, so neither it
/// nor anything it starts can spawn into another coalition. Then every
/// descendant, however detached, stays a member.
fn dedicated_coalition() -> Option<u64> {
    #[cfg(target_os = "macos")]
    {
        // SAFETY: plain geteuid.
        if unsafe { libc::geteuid() } == 0 {
            return None;
        }
        let me = std::process::id();
        let id = crate::platform::coalition(me)?;
        (crate::platform::coalition_members(id) == [me]).then_some(id)
    }
    #[cfg(not(target_os = "macos"))]
    None
}

/// macOS has no subreaper: an orphan is reparented to launchd. Kill every
/// process that is not this one or its descendant, yet is a member of this
/// daemon's sealed coalition (it descended from a component, however it
/// detached), or is an orphan of this user still carrying a supervised state
/// root's marker (without a sealed coalition, a descendant that cleared its
/// environment escapes this). Returns how many were killed.
#[cfg(target_os = "macos")]
pub fn sweep_orphans() -> usize {
    let me = std::process::id();
    // Held across the scan, so no child is spawned and registered meanwhile.
    let _known = CHILDREN.lock().unwrap();
    let coalition = *COALITION.lock().unwrap();
    let markers: Vec<String> = MARKERS.lock().unwrap().iter().map(|m| format!("{MARKER}={m}")).collect();
    // SAFETY: plain getuid.
    let uid = unsafe { libc::getuid() };
    let table = processes();
    let mut mine = BTreeSet::from([me]);
    loop {
        let more: Vec<u32> = table.iter().filter(|p| !mine.contains(&p.pid) && mine.contains(&p.ppid)).map(|p| p.pid).collect();
        if more.is_empty() {
            break;
        }
        mine.extend(more);
    }
    let mut killed = 0;
    for p in table.iter().filter(|p| !mine.contains(&p.pid)) {
        let orphan = in_coalition(p.pid, coalition)
            || (p.ppid == 1
                && crate::platform::owner(p.pid) == Some(uid)
                && markers.iter().any(|entry| crate::platform::carries(p.pid, entry.as_bytes())));
        if orphan {
            // SAFETY: plain kill of a process of this user.
            unsafe {
                libc::kill(p.pid as i32, libc::SIGKILL);
            }
            killed += 1;
        }
    }
    killed
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn sweep_orphans() -> usize {
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{ApplyConfig, ApplyMode};

    struct Fixture {
        dir: PathBuf,
        runner: ApplyRunner,
        deployments: u32,
    }
    impl Fixture {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("suifleet-supervisor-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            let runner = ApplyRunner::new(ApplyConfig {
                command: "/usr/bin/true".into(),
                timeout_seconds: 5,
                allow_recipe_commands: true,
                mode: ApplyMode::Direct,
                firmware_spool: "/nonexistent".into(),
                max_memory_bytes: None,
            });
            Self { dir, runner, deployments: 0 }
        }
        fn open(&self) -> Supervisor {
            self.open_with(5)
        }
        fn open_with(&self, max_restarts: u32) -> Supervisor {
            Supervisor::open(
                &self.dir,
                Settings {
                    startup: Duration::from_secs(1),
                    ready_timeout: Duration::from_secs(3),
                    stop_timeout: Duration::from_secs(2),
                    max_restarts,
                    max_memory_bytes: None,
                    overlay_dir: self.dir.join("overlay"),
                },
            )
            .unwrap()
        }
        fn spec(&mut self, version: &str, run: &str) -> Spec {
            self.deployments += 1;
            Spec {
                name: "app".into(),
                version: version.into(),
                deployment: format!("0x{}", self.deployments),
                run: run.into(),
                shutdown: None,
                readiness: None,
                restart: Restart::OnFailure,
                after: Vec::new(),
            }
        }
        async fn deploy(&self, supervisor: &mut Supervisor, spec: Spec, install: Option<&str>) -> anyhow::Result<()> {
            let record = configuration::Record { deployment: spec.deployment.clone(), version: spec.version.clone(), ..Default::default() };
            self.deploy_configured(supervisor, spec, install, &record).await
        }
        async fn deploy_configured(
            &self,
            supervisor: &mut Supervisor,
            spec: Spec,
            install: Option<&str>,
            record: &configuration::Record,
        ) -> anyhow::Result<()> {
            let artifact = self.dir.join(format!("download-{}", spec.deployment));
            std::fs::write(&artifact, format!("artifact {}", spec.version)).unwrap();
            supervisor.deploy(spec, &artifact, install, &self.runner, "test", record).await
        }
        fn work(&self, file: &str) -> PathBuf {
            self.dir.join("supervised/app/work").join(file)
        }
        fn read(&self, file: &str) -> String {
            std::fs::read_to_string(self.work(file)).unwrap_or_default()
        }
        fn active(&self) -> BTreeMap<String, Spec> {
            std::fs::read(self.dir.join("supervised/active.json"))
                .map(|bytes| serde_json::from_slice(&bytes).unwrap())
                .unwrap_or_default()
        }
    }

    fn alive(pid: u32) -> bool {
        start_time(pid).is_some()
    }

    /// Run a command in a new session (macOS has no setsid(1)).
    const SETSID: &str = if cfg!(target_os = "macos") { "/usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV'" } else { "setsid" };

    async fn until(what: &str, mut condition: impl FnMut() -> bool) {
        for _ in 0..100 {
            if condition() {
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        panic!("timed out waiting for {what}");
    }

    fn phase(supervisor: &Supervisor) -> Phase {
        supervisor.status()["app"].phase
    }

    #[tokio::test]
    async fn local_management_preserves_dependency_order_and_committed_versions() {
        let mut f = Fixture::new("local-control");
        let mut s = f.open();
        let dependency = f.spec("1.0.0", "echo dependency; exec sleep 300");
        f.deploy(&mut s, dependency, None).await.unwrap();
        let mut dependent = f.spec("1.0.0", "exec sleep 300");
        dependent.name = "consumer".into();
        dependent.after = vec!["app".into()];
        f.deploy(&mut s, dependent, None).await.unwrap();
        let before = s.status()["app"].pid;
        assert!(s.local_control("app", "stop").await.is_err());
        assert!(s.local_control("app", "restart").await.is_err());
        assert_eq!(s.status()["app"].pid, before);
        s.local_control("consumer", "stop").await.unwrap();
        s.local_control("app", "stop").await.unwrap();
        assert!(s.local_control("consumer", "start").await.is_err());
        s.local_control("app", "start").await.unwrap();
        s.local_control("consumer", "start").await.unwrap();
        assert!(s.local_control("missing", "start").await.is_err());
        assert!(s.local_logs("../app").is_err());
        assert_eq!(f.active().len(), 2);
        s.stop_all().await;
    }

    #[tokio::test]
    async fn starts_ready_and_stops_with_its_descendants() {
        let mut f = Fixture::new("lifecycle");
        let mut s = f.open();
        // A background child in the group and one that escapes it with setsid.
        let spec = f.spec(
            "1.0.0",
            &format!("sleep 300 & echo $! > {{work}}/grouped; {SETSID} sleep 300 & echo $! > {{work}}/escaped; cat {{artifact}} > {{work}}/seen; exec sleep 300"),
        );
        f.deploy(&mut s, spec, None).await.unwrap();
        let status = s.status()["app"].clone();
        assert_eq!(status.phase, Phase::Running);
        let main = status.pid.unwrap();
        assert!(alive(main));
        assert_eq!(f.read("seen"), "artifact 1.0.0");
        let grouped: u32 = f.read("grouped").trim().parse().unwrap();
        let escaped: u32 = f.read("escaped").trim().parse().unwrap();
        assert!(alive(grouped) && alive(escaped));
        // Genuine runtime state on disk.
        let on_disk: BTreeMap<String, Status> =
            serde_json::from_slice(&std::fs::read(f.dir.join("supervised/status.json")).unwrap()).unwrap();
        assert_eq!(on_disk["app"].phase, Phase::Running);
        assert_eq!(on_disk["app"].pid, Some(main));

        s.stop_all().await;
        assert_eq!(phase(&s), Phase::Stopped);
        assert!(!alive(main) && !alive(grouped));
        // The escaped process was orphaned into the subreaper and swept.
        until("the escaped descendant to be killed", || !alive(escaped)).await;
    }

    #[tokio::test]
    async fn graceful_shutdown_command_runs_before_kill() {
        let mut f = Fixture::new("shutdown");
        let mut s = f.open();
        let mut spec = f.spec("1.0.0", "trap 'echo graceful > {work}/stopped; exit 0' TERM; while true; do sleep 0.1; done");
        spec.shutdown = Some("echo requested > {work}/request; kill -TERM {pid}".into());
        f.deploy(&mut s, spec, None).await.unwrap();
        s.stop_all().await;
        assert_eq!(f.read("request").trim(), "requested");
        assert_eq!(f.read("stopped").trim(), "graceful");
    }

    #[tokio::test]
    async fn readiness_gates_applied_and_failure_is_reported() {
        let mut f = Fixture::new("readiness");
        let mut s = f.open();
        let mut spec = f.spec("1.0.0", "sleep 1; touch ready; exec sleep 300");
        spec.readiness = Some("test -e ready".into());
        f.deploy(&mut s, spec, None).await.unwrap();
        assert!(f.work("ready").exists(), "Applied before the readiness check passed");
        s.stop_all().await;

        let mut g = Fixture::new("not-ready");
        let mut s = g.open();
        let mut spec = g.spec("1.0.0", "exec sleep 300");
        spec.readiness = Some("false".into());
        let error = g.deploy(&mut s, spec, None).await.unwrap_err();
        assert!(format!("{error:#}").contains("readiness check did not pass"), "{error:#}");
        assert_eq!(phase(&s), Phase::Stopped);
        assert!(g.active().is_empty(), "a version that never became ready was committed");
    }

    #[tokio::test]
    async fn crash_restarts_with_backoff_until_the_budget_is_exhausted() {
        let mut f = Fixture::new("restart");
        let mut s = f.open_with(2);
        // Ready (survives the 1s startup window), then crashes.
        let spec = f.spec("1.0.0", "echo run >> {work}/runs; sleep 1.2; exit 3");
        f.deploy(&mut s, spec, None).await.unwrap();
        until("the restart budget to run out", || phase(&s) == Phase::Broken).await;
        let status = s.status()["app"].clone();
        assert_eq!(f.read("runs").lines().count(), 3, "initial run plus two restarts");
        assert_eq!(status.restarts, 2);
        assert!(status.last_exit.unwrap().contains("exited with code 3; restart budget exhausted"));
    }

    #[tokio::test]
    async fn restart_policies_never_and_always() {
        let mut f = Fixture::new("never");
        let mut s = f.open();
        let mut spec = f.spec("1.0.0", "echo run >> {work}/runs; sleep 1.2; exit 1");
        spec.restart = Restart::Never;
        f.deploy(&mut s, spec, None).await.unwrap();
        until("the run to end", || phase(&s) == Phase::Exited).await;
        tokio::time::sleep(Duration::from_millis(1500)).await;
        assert_eq!(f.read("runs").lines().count(), 1);

        let mut g = Fixture::new("always");
        let mut s = g.open();
        let mut spec = g.spec("1.0.0", "echo run >> {work}/runs; sleep 1.2; exit 0");
        spec.restart = Restart::Always;
        g.deploy(&mut s, spec, None).await.unwrap();
        until("a clean exit to be restarted", || g.read("runs").lines().count() >= 2).await;
        s.stop_all().await;

        let mut h = Fixture::new("on-failure");
        let mut s = h.open();
        let spec = h.spec("1.0.0", "echo run >> {work}/runs; sleep 1.2; exit 0");
        h.deploy(&mut s, spec, None).await.unwrap();
        until("a clean exit to end the run", || phase(&s) == Phase::Exited).await;
        assert_eq!(h.read("runs").lines().count(), 1);
    }

    #[tokio::test]
    async fn replacement_commits_and_failed_replacement_rolls_back() {
        let mut f = Fixture::new("replace");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", "echo 1 > {work}/version; exec sleep 300");
        f.deploy(&mut s, v1.clone(), None).await.unwrap();
        let first = s.status()["app"].pid.unwrap();

        // A version whose install fails never touches the running one.
        let broken_install = f.spec("1.5.0", "exec sleep 300");
        assert!(f.deploy(&mut s, broken_install, Some("exit 9")).await.is_err());
        assert_eq!(s.status()["app"].pid, Some(first));

        // A version that never becomes ready is rolled back.
        let v2 = f.spec("2.0.0", "echo 2 > {work}/version; exit 1");
        let error = f.deploy(&mut s, v2, None).await.unwrap_err();
        assert!(format!("{error:#}").contains("rolled back to 1.0.0 (ready)"), "{error:#}");
        assert!(!alive(first), "the old run is replaced by a fresh start");
        assert_eq!(phase(&s), Phase::Running);
        assert_eq!(f.read("version").trim(), "1");
        assert_eq!(f.active()["app"], v1);

        // Successful replacements keep only the committed and previous versions.
        let v3 = f.spec("3.0.0", "echo 3 > {work}/version; cat {artifact} > {work}/artifact; exec sleep 300");
        f.deploy(&mut s, v3.clone(), Some("echo installed > {artifacts}/installed")).await.unwrap();
        assert_eq!(f.read("version").trim(), "3");
        assert_eq!(f.read("artifact"), "artifact 3.0.0");
        assert_eq!(f.active()["app"], v3);
        let v4 = f.spec("4.0.0", "exec sleep 300");
        f.deploy(&mut s, v4, None).await.unwrap();
        let mut versions: Vec<String> = std::fs::read_dir(f.dir.join("supervised/app/versions"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().split('-').next().unwrap().to_string())
            .collect();
        versions.sort();
        assert_eq!(versions, ["3.0.0", "4.0.0"]);
        s.stop_all().await;
    }

    #[tokio::test]
    async fn explicit_recovery_restores_previous_artifact_configuration_and_restart() {
        let mut f = Fixture::new("explicit-recovery");
        let mut s = f.open();
        let state = f.dir.join("application");
        std::fs::create_dir_all(&state).unwrap();
        let current = quote(&state.join("current"));
        let config = quote(&state.join("config"));
        s.configure_recovery(BTreeMap::from([("app".into(), format!(
            "cp {{artifact}} {current}; cp {{artifacts}}/config {config}; cat {{configuration}}; echo recovery-output >&2"
        ))]));
        let mut v1 = f.spec("1.0.0", "exec sleep 300");
        v1.readiness = Some(format!("test \"$(cat {current})\" = 'artifact 1.0.0'; test \"$(cat {config})\" = stable"));
        f.deploy(&mut s, v1.clone(), Some(&format!(
            "echo stable > {{artifacts}}/config; cp {{artifact}} {current}; cp {{artifacts}}/config {config}"
        ))).await.unwrap();
        // A partially destructive install supplies failure explicitly.
        let v2 = f.spec("2.0.0", "exec sleep 300");
        assert!(f.deploy(&mut s, v2, Some(&format!("echo broken > {current}; echo broken > {config}; exit 9"))).await.is_err());
        assert_eq!(std::fs::read_to_string(state.join("current")).unwrap(), "artifact 1.0.0");
        assert_eq!(std::fs::read_to_string(state.join("config")).unwrap().trim(), "stable");
        assert!(s.restored("app", &v1.deployment));
        // The recovery command receives the previous version's configuration,
        // and its output goes to the private component log.
        let log = std::fs::read_to_string(f.dir.join("supervised/app/component.log")).unwrap();
        assert!(log.contains("{}") && log.contains("recovery-output"), "{log}");
        // A readiness/health failure invokes the same explicit recovery.
        let mut v3 = f.spec("3.0.0", "exec sleep 300");
        v3.readiness = Some("exit 1".into());
        let error = f.deploy(&mut s, v3, Some(&format!("echo broken > {current}; echo broken > {config}"))).await.unwrap_err();
        assert!(format!("{error:#}").contains("rolled back to 1.0.0 (ready)"));
        assert_eq!(std::fs::read_to_string(state.join("current")).unwrap(), "artifact 1.0.0");
        assert_eq!(std::fs::read_to_string(state.join("config")).unwrap().trim(), "stable");
        assert_eq!(f.active()["app"], v1);
        s.stop_all().await;
        drop(s);
        let mut s = f.open();
        s.bootstrap().await;
        assert!(s.restored("app", &v1.deployment));
        s.stop_all().await;
    }

    #[tokio::test]
    async fn explicit_local_stop_of_broken_component_prevents_recovery() {
        let mut f = Fixture::new("local-stop-broken");
        let mut s = f.open_with(0);
        let v1 = f.spec("1.0.0", "exec sleep 300");
        f.deploy(&mut s, v1, None).await.unwrap();
        let trigger = f.dir.join("crash");
        let v2 = f.spec("2.0.0", &format!("while test ! -e {}; do sleep 0.05; done; exit 9", quote(&trigger)));
        f.deploy(&mut s, v2.clone(), None).await.unwrap();
        let marker = f.dir.join("must-not-recover");
        s.configure_recovery(BTreeMap::from([("app".into(), format!("touch {}", quote(&marker)))]));
        std::fs::write(trigger, "crash").unwrap();
        until("runtime failure", || phase(&s) == Phase::Broken).await;
        s.local_control("app", "stop").await.unwrap();
        assert_eq!(phase(&s), Phase::Stopped);
        s.recover_broken(&f.runner, |_| true).await.unwrap();
        assert!(!marker.exists());
        assert_eq!(s.committed("app"), Some(v2.deployment.as_str()));
        s.stop_all().await;
    }

    #[tokio::test]
    async fn runtime_failure_recovers_retained_previous_version_once() {
        let mut f = Fixture::new("runtime-recovery");
        let mut s = f.open_with(0);
        let v1 = f.spec("1.0.0", "echo 1 > {work}/version; exec sleep 300");
        f.deploy(&mut s, v1.clone(), None).await.unwrap();
        let trigger = f.dir.join("crash");
        let v2 = f.spec("2.0.0", &format!("echo 2 > {{work}}/version; while test ! -e {}; do sleep 0.05; done; exit 9", quote(&trigger)));
        f.deploy(&mut s, v2, None).await.unwrap();
        s.stop_all().await;
        drop(s);
        // Retained prior lifecycle and artifact survive a daemon restart.
        let mut s = f.open_with(0);
        let marker = f.dir.join("recovery-runs");
        s.configure_recovery(BTreeMap::from([("app".into(), format!("cat {{artifact}} > {}; echo ran >> {}", quote(&f.dir.join("restored")), quote(&marker)))]));
        s.bootstrap().await;
        std::fs::write(trigger, "crash").unwrap();
        until("runtime failure", || phase(&s) == Phase::Broken).await;
        s.recover_broken(&f.runner, |_| true).await.unwrap();
        assert_eq!(f.read("version").trim(), "1");
        assert_eq!(std::fs::read_to_string(f.dir.join("restored")).unwrap(), "artifact 1.0.0");
        assert!(s.restored("app", &v1.deployment));
        assert_eq!(s.recovered()["app"], v1.deployment);
        s.recover_broken(&f.runner, |_| true).await.unwrap();
        assert_eq!(std::fs::read_to_string(marker).unwrap(), "ran\n");
        s.stop_all().await;
    }

    #[tokio::test]
    async fn previous_artifact_corruption_refuses_recovery_and_bootstrap() {
        let mut f = Fixture::new("corrupt-recovery");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", "exec sleep 300");
        f.deploy(&mut s, v1.clone(), None).await.unwrap();
        std::fs::write(s.workspace(&v1).join("artifact"), "corrupt").unwrap();
        let marker = f.dir.join("must-not-run");
        s.configure_recovery(BTreeMap::from([("app".into(), format!("touch {}", quote(&marker)))]));
        let v2 = f.spec("2.0.0", "exit 1");
        assert!(f.deploy(&mut s, v2, None).await.unwrap_err().to_string().contains("digest mismatch"));
        assert!(!marker.exists());
        assert!(f.dir.join("supervised/recovery-intent.json").exists());
        s.stop_all().await;
    }

    #[tokio::test]
    async fn interrupted_recovery_keeps_intent_without_repeating_work() {
        let mut f = Fixture::new("interrupted-recovery");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", "exec sleep 300");
        f.deploy(&mut s, v1, None).await.unwrap();
        let marker = f.dir.join("recovery-runs");
        s.configure_recovery(BTreeMap::from([("app".into(), format!("echo ran >> {}; exec sleep 300", quote(&marker)))]));
        let v2 = f.spec("2.0.0", "exit 1");
        tokio::select! {
            _ = f.deploy(&mut s, v2, None) => panic!("recovery should still be running"),
            _ = until("recovery command", || marker.exists()) => {}
        }
        assert!(f.dir.join("supervised/recovery-intent.json").exists());
        s.stop_all().await;
        let settings = s.settings.clone();
        drop(s);
        assert!(Supervisor::open(&f.dir, settings).err().unwrap().to_string().contains("recovery was interrupted or failed"));
        assert_eq!(std::fs::read_to_string(marker).unwrap(), "ran\n");
    }

    #[tokio::test]
    async fn new_failed_install_invalidates_prior_runtime_recovery() {
        for (label, install, indeterminate, supervised) in [
            ("killed", "kill -KILL $$", true, true),
            ("failed", "exit 9", false, true),
            ("direct-killed", "kill -KILL $$", true, false),
            ("direct-failed", "exit 9", false, false),
        ] {
            let mut f = Fixture::new(&format!("stale-recovery-{label}"));
            let mut s = f.open_with(0);
            let v1 = f.spec("1.0.0", "exec sleep 300");
            f.deploy(&mut s, v1.clone(), None).await.unwrap();
            let v2 = f.spec("2.0.0", "exec sleep 300");
            f.deploy(&mut s, v2, None).await.unwrap();
            s.configure_recovery(BTreeMap::from([("app".into(), "true".into())]));
            let pid = s.status()["app"].pid.unwrap();
            unsafe { libc::kill(pid as i32, libc::SIGKILL); }
            until("runtime failure", || phase(&s) == Phase::Broken).await;
            s.recover_broken(&f.runner, |_| true).await.unwrap();
            assert_eq!(s.recovered()["app"], v1.deployment);
            assert!(s.restored("app", &v1.deployment));
            s.configure_recovery(BTreeMap::new());

            let v3 = f.spec("3.0.0", "exec sleep 300");
            let error = if supervised {
                f.deploy(&mut s, v3, Some(install)).await.unwrap_err()
            } else {
                // The admission path also invalidates recovery when replacing
                // supervision with an install-only recipe.
                s.begin_install("app").unwrap();
                let artifact = f.dir.join("direct-artifact");
                std::fs::write(&artifact, "v3").unwrap();
                f.runner.apply(&artifact, Some(install), "test", None, None).await.unwrap_err()
            };
            assert_eq!(error.downcast_ref::<crate::firmware::Indeterminate>().is_some(), indeterminate);
            assert!(error.downcast_ref::<Recovered>().is_none());
            // The retained process is still running, but polling must have no
            // stale recovery identity with which to confirm the failed install.
            assert!(s.restored("app", &v1.deployment));
            assert!(!s.recovered().contains_key("app"));
            s.stop_all().await;
            drop(s);
            let s = f.open_with(0);
            assert!(!s.recovered().contains_key("app"), "stale recovery survived restart");
        }
    }

    #[tokio::test]
    async fn ambiguous_install_never_triggers_destructive_recovery() {
        let mut f = Fixture::new("unknown-install");
        let mut s = f.open_with(0);
        let v1 = f.spec("1.0.0", "exec sleep 300");
        f.deploy(&mut s, v1, None).await.unwrap();
        let marker = f.dir.join("must-not-recover");
        s.configure_recovery(BTreeMap::from([("app".into(), format!("touch {}", quote(&marker)))]));
        let v2 = f.spec("2.0.0", "exec sleep 300");
        let error = f.deploy(&mut s, v2, Some("kill -KILL $$")).await.unwrap_err();
        assert!(error.downcast_ref::<crate::firmware::Indeterminate>().is_some());
        assert!(!marker.exists());
        let pid = s.status()["app"].pid.unwrap();
        unsafe { libc::kill(pid as i32, libc::SIGKILL); }
        until("previous run failure after unknown install", || phase(&s) == Phase::Broken).await;
        s.recover_broken(&f.runner, |_| false).await.unwrap();
        assert!(!marker.exists(), "unconfirmed application state must block runtime recovery too");
        s.stop_all().await;
    }

    #[tokio::test]
    async fn failed_recovery_blocks_restart_and_is_never_repeated() {
        let mut f = Fixture::new("failed-recovery");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", "exec sleep 300");
        f.deploy(&mut s, v1, None).await.unwrap();
        let marker = f.dir.join("recovery-runs");
        s.configure_recovery(BTreeMap::from([("app".into(), format!("echo ran >> {}; exit 8", quote(&marker)))]));
        let v2 = f.spec("2.0.0", "exit 1");
        assert!(f.deploy(&mut s, v2, None).await.unwrap_err().to_string().contains("recovery failed"));
        s.stop_all().await;
        drop(s);
        for _ in 0..2 {
            let settings = Settings { startup: Duration::ZERO, ready_timeout: Duration::from_secs(1), stop_timeout: Duration::from_secs(1), max_restarts: 0, max_memory_bytes: None, overlay_dir: f.dir.join("overlay") };
            let error = Supervisor::open(&f.dir, settings).err().expect("restarted over ambiguous recovery");
            assert!(error.to_string().contains("recovery was interrupted or failed"));
        }
        assert_eq!(std::fs::read_to_string(marker).unwrap(), "ran\n");
    }

    #[tokio::test]
    async fn bootstrap_restarts_the_committed_version_after_a_restart() {
        let mut f = Fixture::new("bootstrap");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", "echo run >> {work}/runs; exec sleep 300");
        f.deploy(&mut s, v1, None).await.unwrap();
        s.stop_all().await;
        drop(s);
        // Daemon (or device) restart: a new supervisor over the same state.
        let mut s = f.open();
        s.bootstrap().await;
        assert_eq!(phase(&s), Phase::Running);
        assert_eq!(s.status()["app"].version, "1.0.0");
        assert_eq!(f.read("runs").lines().count(), 2);
        s.stop_all().await;
    }

    #[tokio::test]
    async fn runs_left_by_a_killed_daemon_are_stopped_before_restarting() {
        let mut f = Fixture::new("crashed-daemon");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", "sleep 300 & echo $! > {work}/grouped; exec sleep 300");
        f.deploy(&mut s, v1, None).await.unwrap();
        let old = s.status()["app"].pid.unwrap();
        let grouped: u32 = f.read("grouped").trim().parse().unwrap();
        // Simulate a daemon killed outright: forget the runs without stopping them.
        for (_, handle) in std::mem::take(&mut s.running) {
            handle.task.abort();
            let _ = handle.task.await;
        }
        // (Dropping the task's handle kills the run itself; its group remains.)
        assert!(alive(grouped));
        // The test process shares its cgroup with others, so nothing proves
        // that no descendant escaped: the leftovers it can find are killed,
        // then it refuses to start, and keeps refusing.
        for _ in 0..2 {
            let error = Supervisor::open(&f.dir, s.settings.clone()).err().expect("started over an unproven leftover");
            assert!(format!("{error:#}").contains("refusing to start components"), "{error:#}");
            assert!(!alive(old) && !alive(grouped), "the previous run survived a restart");
        }
        // An operator who made sure nothing remains clears the record.
        std::fs::remove_file(f.dir.join("supervised").join(INSTANCE)).unwrap();
        let mut s = f.open();
        s.bootstrap().await;
        let new = s.status()["app"].pid.unwrap();
        assert_ne!(new, old);
        s.stop_all().await;
    }

    #[tokio::test]
    async fn recovery_finds_marked_runs_and_their_descendants() {
        let f = Fixture::new("marked");
        let root = f.dir.join("supervised");
        std::fs::create_dir_all(&root).unwrap();
        // A leftover run (no status.json records it) with a setsid-escaped
        // child and one that cleared its environment; and an unrelated process.
        let script = format!(
            "{SETSID} /bin/sh -c 'echo $$ > escaped.ready; exec sleep 300' & echo $! > escaped; \
             env -i /bin/sh -c 'echo $$ > cleared.ready; exec sleep 300' & echo $! > cleared; exec sleep 300"
        );
        let mut leftover = tokio::process::Command::new("/bin/sh");
        leftover
            .arg("-c")
            .arg(script)
            .current_dir(&f.dir)
            .env(MARKER, marker(&root));
        let mut leftover = spawn_tracked(&mut leftover).unwrap();
        let mut other = tokio::process::Command::new("sleep");
        other.arg("300").env(MARKER, "another-supervisor");
        let mut other = spawn_tracked(&mut other).unwrap();
        let read = |file: &str| -> u32 {
            std::fs::read_to_string(f.dir.join(file))
                .unwrap_or_default()
                .trim()
                .parse()
                .unwrap_or(0)
        };
        until("the leftover's children to start", || {
            read("escaped") > 0 && read("cleared") > 0
        })
        .await;
        let (escaped, cleared) = (read("escaped"), read("cleared"));
        // Parent-written PIDs prove fork, not exec. Each child signals only
        // after setsid/env clearing, so slow CI cannot race the assertion.
        until("the children to exec", || {
            read("escaped.ready") == escaped
                && read("cleared.ready") == cleared
                && carries(escaped, &marker(&root))
                && alive(cleared)
                && !carries(cleared, &marker(&root))
        })
        .await;
        assert!(!carries(cleared, &marker(&root)));

        assert!(reap_previous_runs(&root, &BTreeMap::new(), None).unwrap() >= 3);
        assert!(
            !alive(leftover.pid()) && !alive(escaped) && !alive(cleared),
            "a leftover survived recovery"
        );
        let _ = leftover.wait().await;
        assert!(
            alive(other.pid()),
            "recovery killed another supervisor's process"
        );
        unsafe {
            libc::kill(other.pid() as i32, libc::SIGKILL);
        }
        let _ = other.wait().await;
    }

    #[tokio::test]
    async fn a_daemon_killed_during_a_first_install_is_not_taken_for_idle() {
        let mut f = Fixture::new("install-crash");
        let s = f.open();
        let settings = s.settings.clone();
        // A daemon that started nothing restarts without any proof needed,
        // even though it never stopped cleanly.
        drop(s);
        let mut s = f.open();
        // Killed while the first install runs: no status was ever recorded.
        let v1 = f.spec("1.0.0", "exec sleep 300");
        let started = f.dir.join("installing");
        let install = format!("echo $$ > {}; exec sleep 300", quote(&started));
        let deploy = f.deploy(&mut s, v1, Some(&install));
        tokio::select! {
            _ = deploy => panic!("the install finished"),
            _ = until("the install to start", || started.exists()) => {}
        }
        let status = std::fs::read(f.dir.join("supervised/status.json")).unwrap();
        assert_eq!(serde_json::from_slice::<BTreeMap<String, Status>>(&status).unwrap().len(), 0);
        let record: Instance = serde_json::from_slice(&std::fs::read(f.dir.join("supervised").join(INSTANCE)).unwrap()).unwrap();
        assert!(!record.idle, "the install started before its record was marked");
        drop(s);
        for _ in 0..2 {
            let error = Supervisor::open(&f.dir, settings.clone()).err().expect("started over a crashed install");
            assert!(format!("{error:#}").contains("refusing to start components"), "{error:#}");
        }
        // So does an unreadable record.
        std::fs::write(f.dir.join("supervised").join(INSTANCE), b"{").unwrap();
        assert!(Supervisor::open(&f.dir, settings.clone()).is_err(), "an unreadable record proved nothing ran");
        std::fs::remove_file(f.dir.join("supervised").join(INSTANCE)).unwrap();
        f.open().stop_all().await;
    }

    #[tokio::test]
    async fn another_live_instance_is_not_disturbed() {
        let mut f = Fixture::new("second-instance");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", "exec sleep 300");
        f.deploy(&mut s, v1, None).await.unwrap();
        let run = s.status()["app"].pid.unwrap();
        // Recorded as run by another live process (the parent of this one).
        let mut other = Instance::current();
        other.pid = std::os::unix::process::parent_id();
        other.pid_start = start_time(other.pid);
        let record = f.dir.join("supervised").join(INSTANCE);
        let own = std::fs::read(&record).unwrap();
        std::fs::write(&record, serde_json::to_vec(&other).unwrap()).unwrap();
        let error = Supervisor::open(&f.dir, s.settings.clone()).err().expect("a second instance started");
        assert!(format!("{error:#}").contains("another daemon instance"), "{error:#}");
        assert!(alive(run), "a second instance killed the first one's run");
        std::fs::write(&record, own).unwrap();
        s.stop_all().await;
        assert!(!record.exists(), "a clean stop kept the instance record");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn leftovers_are_ruled_out_only_by_a_sealed_empty_cgroup_or_a_reboot() {
        let cgroups = std::env::temp_dir().join(format!("suifleet-cgroups-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&cgroups);
        let service = cgroups.join("system.slice/suifleet.service");
        std::fs::create_dir_all(service.join("sub")).unwrap();
        for dir in [&cgroups, &cgroups.join("system.slice")] {
            std::fs::write(dir.join("cgroup.procs"), "1\n2\n").unwrap();
        }
        let current = Instance { pid: 40, pid_start: Some(1), boot_id: Some("b".into()), cgroup: Some("/system.slice/suifleet.service".into()), coalition: None, idle: false };
        let previous = Instance { pid: 30, ..current.clone() };
        let sealed = |_: &Path| false;
        let procs = |dir: &Path, pids: &str| std::fs::write(dir.join("cgroup.procs"), pids).unwrap();

        // Only the new instance left in the old one's sealed cgroup.
        procs(&service, "40\n");
        procs(&service.join("sub"), "");
        assert_eq!(previous.left_nothing(&current, &cgroups, sealed), Ok(()));
        // A survivor, even in a child cgroup.
        procs(&service.join("sub"), "77\n");
        let error = previous.left_nothing(&current, &cgroups, sealed).unwrap_err();
        assert!(error.contains("1 other process"), "{error}");
        // A different boot ended everything.
        let rebooted = Instance { boot_id: Some("a".into()), ..previous.clone() };
        assert_eq!(rebooted.left_nothing(&current, &cgroups, sealed), Ok(()));
        // A cgroup its processes could leave (here the slice) proves nothing.
        procs(&service.join("sub"), "");
        let slice = cgroups.join("system.slice/cgroup.procs");
        let error = previous.left_nothing(&current, &cgroups, |path: &Path| path == slice).unwrap_err();
        assert!(error.contains("could leave"), "{error}");
        // Neither does an unknown cgroup, or a shared one (as in a container).
        let unknown = Instance { cgroup: None, ..previous.clone() };
        assert!(unknown.left_nothing(&current, &cgroups, sealed).is_err());
        let shared = Instance { cgroup: Some("/".into()), ..previous.clone() };
        assert!(shared.left_nothing(&current, &cgroups, sealed).unwrap_err().contains("other process"));
        // A removed cgroup holds nothing.
        let gone = Instance { cgroup: Some("/system.slice/old.service".into()), ..previous.clone() };
        assert_eq!(gone.left_nothing(&current, &cgroups, sealed), Ok(()));
        let _ = std::fs::remove_dir_all(&cgroups);
    }

    #[test]
    fn leftovers_are_ruled_out_only_by_a_sealed_empty_coalition() {
        let current = Instance { pid: 40, pid_start: Some(1), boot_id: Some("b".into()), cgroup: None, coalition: Some(7), idle: false };
        let previous = Instance { pid: 30, ..current.clone() };
        // Only the new instance left in the old one's coalition (launchd reuses
        // a job's coalition), or nothing at all in it.
        assert_eq!(previous.coalition_left_nothing(&current, |id| if id == 7 { vec![40] } else { vec![] }), Ok(()));
        assert_eq!(previous.coalition_left_nothing(&current, |_| vec![]), Ok(()));
        // A survivor, however detached.
        let error = previous.coalition_left_nothing(&current, |_| vec![40, 77]).unwrap_err();
        assert!(error.contains("1 other process"), "{error}");
        // No sealed coalition of its own (shared, or running as root) proves nothing.
        let unsealed = Instance { coalition: None, ..previous.clone() };
        assert!(unsealed.coalition_left_nothing(&current, |_| vec![]).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn a_test_process_has_no_sealed_coalition_of_its_own() {
        // The test runner shares its coalition with its parents: never one to
        // sweep or to prove anything with.
        assert_eq!(dedicated_coalition(), None);
    }

    #[tokio::test]
    async fn readiness_and_shutdown_keep_the_component_environment() {
        std::env::set_var("SUIFLEET_TEST_SECRET", "leak");
        let mut f = Fixture::new("helper-env");
        let mut s = f.open();
        let mut spec = f.spec("1.0.0", "trap 'exit 0' TERM; while true; do sleep 0.1; done");
        let check = "test \"$SUIFLEET_COMPONENT/$SUIFLEET_COMPONENT_VERSION\" = app/1.0.0";
        spec.readiness = Some(format!("{check} && env > {{work}}/readiness"));
        spec.shutdown = Some(format!("{check} && env > {{work}}/shutdown; kill -TERM {{pid}}"));
        f.deploy(&mut s, spec, None).await.unwrap();
        s.stop_all().await;
        let entry = format!("{MARKER}={}", marker(&f.dir.join("supervised")));
        for helper in ["readiness", "shutdown"] {
            let env = f.read(helper);
            assert!(env.lines().any(|line| line == entry), "{helper} lost the recovery marker: {env}");
            assert!(!env.contains("SUIFLEET_TEST"), "{helper} inherited the daemon environment: {env}");
        }
    }

    #[tokio::test]
    async fn retired_component_is_stopped_and_not_restarted() {
        let mut f = Fixture::new("retire");
        let mut s = f.open();
        let v1 = f.spec("1.0.0", &format!("echo run >> {{work}}/runs; {SETSID} sleep 300 & echo $! > {{work}}/escaped; exec sleep 300"));
        f.deploy(&mut s, v1, None).await.unwrap();
        let main = s.status()["app"].pid.unwrap();
        let escaped: u32 = f.read("escaped").trim().parse().unwrap();
        assert!(s.supervises("app"));
        s.retire("app").await.unwrap();
        assert!(!s.supervises("app"));
        assert!(!alive(main));
        until("the escaped descendant to be killed", || !alive(escaped)).await;
        assert!(f.active().is_empty(), "a retired version stayed committed");
        assert!(!s.status().contains_key("app"));
        assert!(!f.dir.join("supervised/app/versions").exists());
        s.stop_all().await;
        drop(s);
        let mut s = f.open();
        s.bootstrap().await;
        assert!(s.status().is_empty());
        assert_eq!(f.read("runs").lines().count(), 1, "a retired version started again");
    }

    #[test]
    fn bootstrap_starts_dependencies_first() {
        let spec = |name: &str, after: &[&str]| Spec {
            name: name.into(),
            version: "1".into(),
            deployment: "0x1".into(),
            run: "true".into(),
            shutdown: None,
            readiness: None,
            restart: Restart::Never,
            after: after.iter().map(|a| a.to_string()).collect(),
        };
        let active: BTreeMap<String, Spec> = [spec("app", &["db", "net"]), spec("db", &["net"]), spec("net", &[]), spec("aux", &["gone"])]
            .into_iter()
            .map(|s| (s.name.clone(), s))
            .collect();
        let order: Vec<String> = start_order(&active).into_iter().map(|s| s.name).collect();
        assert_eq!(order, ["aux", "net", "db", "app"]);
    }

    #[tokio::test]
    async fn rejects_unsafe_names() {
        let mut f = Fixture::new("names");
        let mut s = f.open();
        let mut spec = f.spec("../escape", "true");
        assert!(f.deploy(&mut s, spec.clone(), None).await.is_err());
        spec.version = "1.0.0".into();
        spec.name = "a/b".into();
        assert!(f.deploy(&mut s, spec, None).await.is_err());
    }

    fn configured(spec: &Spec, effective: serde_json::Value, confidential: &[&str]) -> configuration::Record {
        configuration::Record {
            deployment: spec.deployment.clone(),
            version: spec.version.clone(),
            effective: effective.as_object().unwrap().clone(),
            confidential: confidential.iter().map(|p| p.to_string()).collect(),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn runs_with_its_configuration_across_restarts_and_rollback() {
        use std::os::unix::fs::PermissionsExt;
        let mut f = Fixture::new("configuration");
        let overlay = f.dir.join("overlay");
        std::fs::create_dir_all(&overlay).unwrap();
        let secret = overlay.join("app.json");
        std::fs::write(&secret, r#"{"/db/password": "one"}"#).unwrap();
        std::fs::set_permissions(&secret, std::fs::Permissions::from_mode(0o600)).unwrap();
        let mut s = f.open();
        let run = "cp {configuration} {work}/seen.json; echo \"$SUIFLEET_CONFIGURATION\" > {work}/env; exec sleep 300";
        let v1 = f.spec("1.0.0", run);
        let record = configured(&v1, serde_json::json!({"port": 1, "db": {}}), &["/db/password"]);
        // Each install script also receives it.
        // Its output, which here prints the configuration, goes to the private
        // component log, never to the daemon's own stdout/stderr.
        let install = "cp {configuration} {artifacts}/installed.json; cat {configuration}; cat {configuration} >&2";
        f.deploy_configured(&mut s, v1.clone(), Some(install), &record).await.unwrap();
        let log = f.dir.join("supervised/app/component.log");
        assert_eq!(std::fs::read_to_string(&log).unwrap().matches("\"one\"").count(), 2);
        assert_eq!(std::fs::metadata(&log).unwrap().permissions().mode() & 0o777, 0o600);
        let seen = |f: &Fixture, file| serde_json::from_str::<serde_json::Value>(&f.read(file)).unwrap();
        until("configuration seen", || f.work("seen.json").exists() && f.work("env").exists()).await;
        assert_eq!(seen(&f, "seen.json"), serde_json::json!({"port": 1, "db": {"password": "one"}}));
        let file = PathBuf::from(f.read("env").trim());
        assert!(file.starts_with(f.dir.join("supervised/app/versions")), "{}", file.display());
        let installed: serde_json::Value = serde_json::from_slice(&std::fs::read(file.with_file_name("installed.json")).unwrap()).unwrap();
        assert_eq!(installed, serde_json::json!({"port": 1, "db": {"password": "one"}}));
        assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
        // The public record holds no confidential value.
        let state = std::fs::read_to_string(file.with_file_name(CONFIGURATION_STATE)).unwrap();
        assert!(!state.contains("one"), "{state}");

        // A version without the confidential value provisioned never starts;
        // the running version, with its own configuration, is restored.
        let v2 = f.spec("2.0.0", run);
        let missing = configured(&v2, serde_json::json!({"port": 2}), &["/api/token"]);
        let error = f.deploy_configured(&mut s, v2, None, &missing).await.unwrap_err().to_string();
        assert!(error.contains("/api/token") && error.contains("not provisioned"), "{error}");
        assert_eq!(f.active()["app"], v1);
        assert_eq!(seen(&f, "seen.json")["port"], 1);

        // A replacement that fails to start rolls back to v1 and its configuration.
        let v3 = f.spec("3.0.0", "cp {configuration} {work}/seen.json; exit 3");
        let error = f.deploy_configured(&mut s, v3.clone(), None, &configured(&v3, serde_json::json!({"port": 3}), &[])).await.unwrap_err();
        assert!(error.to_string().contains("rolled back to 1.0.0"), "{error}");
        until("v1 restarted", || f.work("seen.json").exists() && seen(&f, "seen.json")["port"] == 1).await;

        // A daemon restart starts it again with the rotated overlay value.
        s.stop_all().await;
        std::fs::write(&secret, r#"{"/db/password": "two"}"#).unwrap();
        std::fs::remove_file(f.work("seen.json")).unwrap();
        let mut s = f.open();
        s.bootstrap().await;
        until("restarted", || f.work("seen.json").exists()).await;
        assert_eq!(seen(&f, "seen.json"), serde_json::json!({"port": 1, "db": {"password": "two"}}));
        s.stop_all().await;
        // Each value was recorded for log redaction before a component got it.
        let retained: serde_json::Value = serde_json::from_slice(&std::fs::read(f.dir.join("logs/confidential.json")).unwrap()).unwrap();
        let values: Vec<_> = retained["values"].as_object().unwrap().keys().cloned().collect();
        assert_eq!(values, ["one", "two"]);
    }

    #[tokio::test]
    async fn first_deployment_after_validation_syncs_its_workspace_path_before_install() {
        let mut f = Fixture::new("durable-workspace");
        let mut s = f.open();
        let artifact = f.dir.join("candidate-artifact");
        std::fs::write(&artifact, "artifact").unwrap();
        let candidate = f.dir.join("candidate.json");
        std::fs::write(&candidate, "{}").unwrap();
        // Validation creates supervised/app (its work directory and log)
        // before deploy, without making it durable.
        assert!(s.validate("app", "1.0.0", "true", &artifact, &candidate, Duration::from_secs(5)).await.unwrap());
        assert!(f.dir.join("supervised/app").is_dir());
        configuration::SYNCED.with(|synced| synced.borrow_mut().clear());
        // A failing install returns right after it ran: every sync recorded
        // happened before it.
        let v1 = f.spec("1.0.0", "exec sleep 300");
        let record = configured(&v1, serde_json::json!({}), &[]);
        assert!(f.deploy_configured(&mut s, v1, Some("exit 1"), &record).await.is_err());
        let synced = configuration::SYNCED.with(|synced| synced.take());
        let root = f.dir.join("supervised");
        for dir in [f.dir.clone(), root.clone(), root.join("app"), root.join("app/versions")] {
            assert!(synced.contains(&dir), "{} not synced before install: {synced:?}", dir.display());
        }
    }

    #[tokio::test]
    async fn validate_accepts_defers_or_rejects_without_side_effects() {
        let f = Fixture::new("validate");
        let s = f.open();
        let artifact = f.dir.join("candidate-artifact");
        std::fs::write(&artifact, "artifact").unwrap();
        let candidate = f.dir.join("candidate.json");
        std::fs::write(&candidate, r#"{"port": 80}"#).unwrap();
        let check = |command: &'static str| s.validate("app", "1.0.0", command, &artifact, &candidate, Duration::from_secs(5));
        let script = "test \"$SUIFLEET_CONFIGURATION\" = {configuration} && test -f {artifact} && grep -q 80 {configuration}";
        assert!(check(script).await.unwrap());
        assert!(!check("exit 75").await.unwrap(), "exit 75 defers");
        let error = check("echo 'port 80 is reserved' >&2; exit 1").await.unwrap_err();
        assert!(format!("{error:#}").contains("rejected"), "{error:#}");
        // Its output goes to the component log, not into diagnostics.
        assert!(!format!("{error:#}").contains("reserved"));
        let log = std::fs::read_to_string(f.dir.join("supervised/app/component.log")).unwrap();
        assert!(log.contains("port 80 is reserved"), "{log}");
        let hung = s.validate("app", "1.0.0", "exec sleep 30", &artifact, &candidate, Duration::from_secs(1)).await;
        assert!(format!("{:#}", hung.unwrap_err()).contains("timed out"));
        assert!(s.validate("../x", "1", "true", &artifact, &candidate, Duration::from_secs(1)).await.is_err());
        assert!(s.active.is_empty(), "nothing was installed or committed");
    }
}
