//! Supervised component runtime (SF-014): persistent versioned workspaces,
//! process lifecycle, readiness, restart policy and descendant containment.
//!
//! Layout under `<state_dir>/components`:
//! - `active.json`: the committed version of each component, started again on
//!   every daemon start (bootstrap, reboot). A version is committed only once it
//!   became ready, so an interrupted replacement restarts the previous one.
//! - `status.json`: genuine runtime state, rewritten on every transition.
//! - `<name>/versions/<version>-<deployment>/`: the version's verified artifact
//!   (and whatever its install step wrote). The active and previous versions
//!   are kept for rollback; older ones are removed.
//! - `<name>/work/`: the component's working directory and HOME, kept across
//!   versions; `<name>/component.log`: its stdout/stderr.
//!
//! Run commands must stay in the foreground. Each runs in its own process
//! group, which is killed when it exits or stops; the daemon is also a child
//! subreaper and kills any process that escaped its group and was orphaned.
use crate::apply::{self, ApplyRunner};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
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
}

struct Handle {
    stop: watch::Sender<bool>,
    phase: watch::Receiver<Phase>,
    task: tokio::task::JoinHandle<()>,
}

pub struct Supervisor {
    root: PathBuf,
    settings: Settings,
    active: BTreeMap<String, Spec>,
    running: BTreeMap<String, Handle>,
    status: Arc<Mutex<BTreeMap<String, Status>>>,
}

impl Supervisor {
    pub fn open(state_dir: &Path, settings: Settings) -> anyhow::Result<Self> {
        let root = state_dir.join("components");
        std::fs::create_dir_all(&root)?;
        let active = match std::fs::read(root.join("active.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => BTreeMap::new(),
            Err(error) => return Err(error.into()),
        };
        contain_descendants()?;
        let left = reap_previous_runs(&root);
        if left > 0 {
            warn!(left, "Killed component runs left by a previous daemon instance");
        }
        let supervisor = Self {
            root,
            settings,
            active,
            running: BTreeMap::new(),
            status: Arc::new(Mutex::new(BTreeMap::new())),
        };
        write_status(&supervisor.root, &supervisor.status)?;
        Ok(supervisor)
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
    pub async fn deploy(
        &mut self,
        spec: Spec,
        artifact: &Path,
        install: Option<&str>,
        runner: &ApplyRunner,
        request_id: &str,
    ) -> anyhow::Result<()> {
        anyhow::ensure!(safe_segment(&spec.name), "component name is not a safe path segment");
        anyhow::ensure!(safe_segment(&spec.version), "component version is not a safe path segment");
        let workspace = self.workspace(&spec);
        let _ = std::fs::remove_dir_all(&workspace);
        std::fs::create_dir_all(&workspace)?;
        let installed = workspace.join("artifact");
        std::fs::rename(artifact, &installed)?;
        if let Some(script) = install {
            // The old version keeps running until the new one is installed.
            if let Err(error) = runner.apply(&installed, Some(script), request_id).await {
                let _ = std::fs::remove_dir_all(&workspace);
                return Err(error.context("install failed; the running version is unchanged"));
            }
        }
        let previous = self.active.get(&spec.name).cloned();
        self.stop(&spec.name).await;
        self.start(spec.clone());
        match self.wait_ready(&spec.name).await {
            Ok(()) => {
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
                self.start(previous.clone());
                let restored = match self.wait_ready(&previous.name).await {
                    Ok(()) => "ready".to_string(),
                    Err(error) => format!("not ready: {error:#}"),
                };
                anyhow::bail!(
                    "{error:#}; rolled back to {} ({restored})",
                    previous.version
                )
            }
        }
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
    }

    #[cfg(test)]
    pub fn status(&self) -> BTreeMap<String, Status> {
        self.status.lock().unwrap().clone()
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
        let mut script = template
            .replace("{artifact}", &quote(&self.workspace.join("artifact")))
            .replace("{artifacts}", &quote(&self.workspace))
            .replace("{work}", &quote(&work));
        if let Some(pid) = pid {
            script = script.replace("{pid}", &pid.to_string());
        }
        let mut command = tokio::process::Command::new("/bin/sh");
        command.arg("-c").arg(script);
        apply::harden(&mut command, &work, self.settings.max_memory_bytes);
        command
            .env("SUIFLEET_COMPONENT", &self.spec.name)
            .env("SUIFLEET_COMPONENT_VERSION", &self.spec.version);
        command
    }

    /// A bounded helper command (readiness check or shutdown).
    async fn helper(&self, template: &str, pid: u32, timeout: Duration) -> anyhow::Result<()> {
        let work = self.component.join("work");
        let command = self.command(template, Some(pid));
        apply::run_bounded(command, &work, timeout, self.settings.max_memory_bytes).await
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
    std::fs::OpenOptions::new().create(true).append(true).open(path)
}

fn atomic_write(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    let temporary = path.with_extension("tmp");
    std::fs::write(&temporary, bytes)?;
    std::fs::File::open(&temporary)?.sync_all()?;
    std::fs::rename(&temporary, path)?;
    Ok(())
}

fn write_status(root: &Path, status: &Mutex<BTreeMap<String, Status>>) -> anyhow::Result<()> {
    let bytes = serde_json::to_vec_pretty(&*status.lock().unwrap())?;
    let temporary = root.join("status.json.tmp");
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(&temporary, root.join("status.json"))?;
    Ok(())
}

// --- Descendant containment -------------------------------------------------

/// Kernel start time of a live (non-zombie) process, from /proc.
fn start_time(pid: u32) -> Option<u64> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rest = stat.rsplit_once(')')?.1;
    let fields: Vec<&str> = rest.split_whitespace().collect();
    // Fields 3.. follow the command; starttime is field 22.
    (fields.first() != Some(&"Z")).then_some(())?;
    fields.get(19)?.parse().ok()
}

fn is_zombie(pid: u32) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .and_then(|stat| stat.rsplit_once(')').map(|(_, rest)| rest.split_whitespace().next() == Some("Z")))
        .unwrap_or(false)
}

/// A daemon that died without stopping its components (crash, SIGKILL)
/// leaves their runs behind; kill each recorded run and its process group
/// before starting them again, so no component runs twice.
fn reap_previous_runs(root: &Path) -> usize {
    let Ok(bytes) = std::fs::read(root.join("status.json")) else {
        return 0;
    };
    let Ok(previous) = serde_json::from_slice::<BTreeMap<String, Status>>(&bytes) else {
        return 0;
    };
    let mut killed = 0;
    for status in previous.values() {
        let (Some(pid), Some(started)) = (status.pid, status.pid_start) else {
            continue;
        };
        // Either the recorded run itself, or its group outliving it (Linux
        // does not reuse a PID while it is still a process-group ID); a
        // different process under a reused PID is left alone.
        let current = start_time(pid);
        let gone = !Path::new(&format!("/proc/{pid}")).exists() || is_zombie(pid);
        if current == Some(started) || gone {
            apply::kill_group(pid as i32);
            if !gone {
                unsafe {
                    libc::kill(pid as i32, libc::SIGKILL);
                }
            }
            killed += 1;
        }
    }
    killed
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

/// Become a child subreaper and kill orphans as they arrive.
fn contain_descendants() -> anyhow::Result<()> {
    #[cfg(target_os = "linux")]
    {
        static STARTED: std::sync::Once = std::sync::Once::new();
        // SAFETY: plain prctl on this process.
        if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
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
    }
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
#[cfg(not(target_os = "linux"))]
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
                command: "/bin/true".into(),
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
            let artifact = self.dir.join(format!("download-{}", spec.deployment));
            std::fs::write(&artifact, format!("artifact {}", spec.version)).unwrap();
            supervisor.deploy(spec, &artifact, install, &self.runner, "test").await
        }
        fn work(&self, file: &str) -> PathBuf {
            self.dir.join("components/app/work").join(file)
        }
        fn read(&self, file: &str) -> String {
            std::fs::read_to_string(self.work(file)).unwrap_or_default()
        }
        fn active(&self) -> BTreeMap<String, Spec> {
            std::fs::read(self.dir.join("components/active.json"))
                .map(|bytes| serde_json::from_slice(&bytes).unwrap())
                .unwrap_or_default()
        }
    }

    fn alive(pid: u32) -> bool {
        std::fs::read_to_string(format!("/proc/{pid}/stat"))
            .ok()
            .and_then(|stat| stat.rsplit_once(')').map(|(_, rest)| rest.split_whitespace().next() != Some("Z")))
            .unwrap_or(false)
    }

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
    async fn starts_ready_and_stops_with_its_descendants() {
        let mut f = Fixture::new("lifecycle");
        let mut s = f.open();
        // A background child in the group and one that escapes it with setsid.
        let spec = f.spec(
            "1.0.0",
            "sleep 300 & echo $! > {work}/grouped; setsid sleep 300 & echo $! > {work}/escaped; cat {artifact} > {work}/seen; exec sleep 300",
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
            serde_json::from_slice(&std::fs::read(f.dir.join("components/status.json")).unwrap()).unwrap();
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
        let mut versions: Vec<String> = std::fs::read_dir(f.dir.join("components/app/versions"))
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().split('-').next().unwrap().to_string())
            .collect();
        versions.sort();
        assert_eq!(versions, ["3.0.0", "4.0.0"]);
        s.stop_all().await;
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
        let mut s = f.open();
        assert!(!alive(old) && !alive(grouped), "the previous run survived a restart");
        s.bootstrap().await;
        let new = s.status()["app"].pid.unwrap();
        assert_ne!(new, old);
        s.stop_all().await;
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
}
