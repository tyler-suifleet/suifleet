use crate::config::{ApplyConfig, ApplyMode};
use std::{path::Path, process::Stdio, time::Duration};

/// Search path for updater/recipe children; their environment is otherwise cleared.
const CHILD_PATH: &str = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
const CHILD_MAX_FILES: u64 = 1024;

pub struct ApplyRunner {
    config: ApplyConfig,
}
impl ApplyRunner {
    pub fn new(config: ApplyConfig) -> Self {
        Self { config }
    }
    /// `request_id` names the firmware-helper request; it must be stable for
    /// the deployment so a late helper result can still be collected.
    /// `configuration` is the component's materialized configuration file,
    /// substituted for `{configuration}` in an install script. `output`
    /// receives that script's stdout/stderr instead of the daemon's own,
    /// since it may print the configuration it was given.
    pub async fn apply(
        &self,
        artifact: &Path,
        install: Option<&str>,
        request_id: &str,
        configuration: Option<&Path>,
        output: Option<std::fs::File>,
    ) -> anyhow::Result<()> {
        let workdir = artifact
            .parent()
            .ok_or_else(|| anyhow::anyhow!("artifact has no directory"))?;
        let timeout = Duration::from_secs(self.config.timeout_seconds);
        if let Some(script) = install {
            anyhow::ensure!(
                self.config.allow_recipe_commands,
                "recipe commands disabled"
            );
            // Artifact substitutions are shell-quoted; operator-authored scripts are intentional code.
            // They never reach the privileged firmware helper.
            let path = artifact
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("non-UTF8 artifact path"))?;
            let parent = workdir
                .to_str()
                .ok_or_else(|| anyhow::anyhow!("non-UTF8 artifact directory"))?;
            let mut script = script
                .replace("{artifact}", &shell_words::quote(path))
                .replace("{artifacts}", &shell_words::quote(parent));
            if let Some(configuration) = configuration {
                script = script.replace("{configuration}", &shell_words::quote(&configuration.to_string_lossy()));
            }
            let mut cmd = tokio::process::Command::new("/bin/sh");
            cmd.arg("-c").arg(script);
            if let Some(output) = output {
                cmd.stdout(output.try_clone()?).stderr(output);
            }
            return run_bounded(cmd, workdir, timeout, self.config.max_memory_bytes).await;
        }
        match self.config.mode {
            ApplyMode::Direct => {
                let cmd = command(&self.config.command, artifact)?;
                run_bounded(cmd, workdir, timeout, self.config.max_memory_bytes).await
            }
            ApplyMode::PrivilegedHelper => {
                crate::firmware::submit(&self.config.firmware_spool, request_id, artifact, timeout)
                    .await
            }
        }
    }
}

/// `command` split as shell words, with the artifact appended as the final argument.
pub fn command(command: &str, artifact: &Path) -> anyhow::Result<tokio::process::Command> {
    let parts = shell_words::split(command)?;
    let bin = parts
        .first()
        .ok_or_else(|| anyhow::anyhow!("apply command is empty"))?;
    let mut cmd = tokio::process::Command::new(bin);
    cmd.args(&parts[1..]).arg(artifact);
    Ok(cmd)
}

/// Run an updater/recipe child with a cleared environment, no stdin, its own
/// working directory, no core dumps, bounded descriptors/memory and a timeout
/// that kills the whole process group. Any non-success is an error, so a
/// privilege failure can never be reported as Applied.
pub async fn run_bounded(
    mut command: tokio::process::Command,
    workdir: &Path,
    timeout: Duration,
    max_memory_bytes: Option<u64>,
) -> anyhow::Result<()> {
    harden(&mut command, workdir, max_memory_bytes);
    run_hardened(command, timeout).await
}

/// [`run_bounded`] for a command already passed through [`harden`] (and
/// possibly given deliberate environment variables afterwards).
pub async fn run_hardened(
    command: tokio::process::Command,
    timeout: Duration,
) -> anyhow::Result<()> {
    exit_result(run_status(command, timeout).await?)
}

/// [`run_hardened`], returning how the command exited instead of judging it.
pub async fn run_status(
    mut command: tokio::process::Command,
    timeout: Duration,
) -> anyhow::Result<std::process::ExitStatus> {
    let mut child = crate::supervisor::spawn_tracked(&mut command).map_err(spawn_error)?;
    #[cfg(unix)]
    let _group = ProcessGroup(child.pid() as i32);
    let status = match tokio::time::timeout(timeout, child.wait()).await {
        Ok(status) => status.map_err(|error| crate::firmware::Indeterminate(format!(
            "cannot collect apply command exit result: {error}; completion unknown"
        )))?,
        Err(_) => {
            // Kill and reap the group now rather than leaving it to drop.
            #[cfg(unix)]
            kill_group(child.pid() as i32);
            let _ = child.wait().await;
            return Err(crate::firmware::Indeterminate(format!(
                "apply command timed out after {}s; process group killed; completion unknown",
                timeout.as_secs()
            )).into());
        }
    };
    Ok(status)
}

/// The confinement every updater, recipe and component child gets: a cleared
/// environment, no stdin, its own working directory and process group, no
/// core dumps, bounded descriptors/memory, and killed if its handle is dropped.
pub fn harden(
    command: &mut tokio::process::Command,
    workdir: &Path,
    max_memory_bytes: Option<u64>,
) {
    command
        .env_clear()
        .env("PATH", CHILD_PATH)
        .env("LANG", "C.UTF-8")
        .env("HOME", workdir)
        .current_dir(workdir)
        .stdin(Stdio::null())
        .kill_on_drop(true);
    #[cfg(unix)]
    {
        command.process_group(0);
        // SAFETY: only async-signal-safe getrlimit/setrlimit calls run between fork and exec.
        unsafe {
            command.pre_exec(move || limit_child(max_memory_bytes));
        }
    }
}

/// SIGKILL a child's whole process group (its PID is the group ID).
#[cfg(unix)]
pub fn kill_group(group: i32) {
    unsafe {
        libc::kill(-group, libc::SIGKILL);
    }
}

fn spawn_error(error: std::io::Error) -> anyhow::Error {
    match error.kind() {
        std::io::ErrorKind::PermissionDenied => {
            anyhow::anyhow!("privilege failure: updater is not executable by this user ({error})")
        }
        std::io::ErrorKind::NotFound => anyhow::anyhow!("updater not found ({error})"),
        _ => error.into(),
    }
}

pub fn exit_result(status: std::process::ExitStatus) -> anyhow::Result<()> {
    if status.success() {
        return Ok(());
    }
    match status.code() {
        Some(126) => {
            anyhow::bail!("privilege failure: updater could not execute a command (exit 126)")
        }
        Some(127) => anyhow::bail!("updater command not found (exit 127)"),
        Some(code) => anyhow::bail!("apply command failed with exit code {code}"),
        None => {
            #[cfg(unix)]
            {
                use std::os::unix::process::ExitStatusExt;
                return Err(crate::firmware::Indeterminate(format!("apply command killed by signal {:?}; completion unknown", status.signal())).into());
            }
            #[cfg(not(unix))]
            anyhow::bail!("apply command terminated abnormally")
        }
    }
}

#[cfg(unix)]
fn limit_child(max_memory_bytes: Option<u64>) -> std::io::Result<()> {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    type Resource = libc::__rlimit_resource_t;
    #[cfg(not(all(target_os = "linux", target_env = "gnu")))]
    type Resource = libc::c_int;
    fn set(resource: Resource, limit: u64) -> std::io::Result<()> {
        let mut current = libc::rlimit {
            rlim_cur: 0,
            rlim_max: 0,
        };
        // Only ever lower limits; raising would need privileges and fail.
        if unsafe { libc::getrlimit(resource, &mut current) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        let value = limit.min(current.rlim_max as u64) as libc::rlim_t;
        let new = libc::rlimit {
            rlim_cur: value,
            rlim_max: value,
        };
        if unsafe { libc::setrlimit(resource, &new) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(())
    }
    set(libc::RLIMIT_CORE, 0)?;
    set(libc::RLIMIT_NOFILE, CHILD_MAX_FILES)?;
    if let Some(bytes) = max_memory_bytes {
        set(libc::RLIMIT_AS, bytes)?;
    }
    Ok(())
}

/// Stop all updater descendants on timeout/cancellation, not just its shell.
#[cfg(unix)]
struct ProcessGroup(i32);
#[cfg(unix)]
impl Drop for ProcessGroup {
    fn drop(&mut self) {
        // The child was placed in a fresh process group whose ID is its PID.
        kill_group(self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runner(command: &str, allow_recipe_commands: bool) -> ApplyRunner {
        ApplyRunner::new(ApplyConfig {
            command: command.into(),
            timeout_seconds: 1,
            allow_recipe_commands,
            mode: ApplyMode::Direct,
            firmware_spool: "/nonexistent".into(),
            max_memory_bytes: None,
        })
    }

    fn workdir(name: &str) -> (std::path::PathBuf, std::path::PathBuf) {
        let dir =
            std::env::temp_dir().join(format!("suifleet-apply-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let artifact = dir.join("artifact");
        std::fs::write(&artifact, b"artifact").unwrap();
        (dir, artifact)
    }

    #[tokio::test]
    async fn propagates_apply_failure() {
        let (_dir, artifact) = workdir("false");
        let error = runner("/bin/false", false)
            .apply(&artifact, None, "test", None, None)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("exit code 1"), "{error}");
    }

    #[tokio::test]
    async fn times_out_hung_apply() {
        let (_dir, artifact) = workdir("hung");
        let error = runner("/bin/true", true)
            .apply(&artifact, Some("exec sleep 10"), "test", None, None)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("timed out"), "{error}");
        assert!(error.downcast_ref::<crate::firmware::Indeterminate>().is_some());
    }

    #[tokio::test]
    async fn timeout_kills_background_descendants() {
        let (dir, artifact) = workdir("descendants");
        let marker = dir.join("survivor");
        let script = format!("(sleep 2; touch {}) & exec sleep 10", marker.display());
        assert!(runner("/bin/true", true)
            .apply(&artifact, Some(&script), "test", None, None)
            .await
            .is_err());
        tokio::time::sleep(Duration::from_secs(3)).await;
        assert!(
            !marker.exists(),
            "descendant outlived the killed process group"
        );
    }

    #[tokio::test]
    async fn non_executable_updater_is_a_privilege_failure() {
        let (dir, artifact) = workdir("noexec");
        let updater = dir.join("updater");
        std::fs::write(&updater, "#!/bin/sh\nexit 0\n").unwrap(); // mode 0644: not executable
        let error = runner(updater.to_str().unwrap(), false)
            .apply(&artifact, None, "test", None, None)
            .await
            .unwrap_err();
        assert!(
            error.to_string().starts_with("privilege failure"),
            "{error}"
        );
    }

    #[tokio::test]
    async fn denied_system_write_inside_updater_fails() {
        let (_dir, artifact) = workdir("denied");
        // An unprivileged updater cannot change kernel settings; the denial must surface.
        let error = runner("/bin/true", true)
            .apply(&artifact, Some("echo suifleet > /proc/sys/kernel/hostname"), "test", None, None)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("exit code"), "{error}");
    }

    #[tokio::test]
    async fn exit_126_is_reported_as_privilege_failure() {
        let (_dir, artifact) = workdir("exit126");
        let error = runner("/bin/true", true)
            .apply(&artifact, Some("exit 126"), "test", None, None)
            .await
            .unwrap_err();
        assert!(
            error.to_string().starts_with("privilege failure"),
            "{error}"
        );
    }

    #[tokio::test]
    async fn install_receives_the_configuration_path() {
        let (dir, artifact) = workdir("configuration");
        let configuration = dir.join("my config.json");
        std::fs::write(&configuration, b"{\"port\":1}").unwrap();
        runner("/bin/true", true)
            .apply(&artifact, Some("cp {configuration} seen.json"), "test", Some(&configuration), None)
            .await
            .unwrap();
        assert_eq!(std::fs::read(dir.join("seen.json")).unwrap(), b"{\"port\":1}");
    }

    #[tokio::test]
    async fn install_output_goes_to_the_given_file() {
        let (dir, artifact) = workdir("output");
        let configuration = dir.join("configuration.json");
        std::fs::write(&configuration, b"{\"port\":1}").unwrap();
        let log = dir.join("component.log");
        let output = std::fs::File::create(&log).unwrap();
        runner("/bin/true", true)
            .apply(&artifact, Some("cat {configuration}; echo; cat {configuration} >&2"), "test", Some(&configuration), Some(output))
            .await
            .unwrap();
        assert_eq!(std::fs::read_to_string(&log).unwrap(), "{\"port\":1}\n{\"port\":1}");
    }

    #[tokio::test]
    async fn child_gets_clean_environment_and_limits() {
        let (dir, artifact) = workdir("env");
        std::env::set_var("SUIFLEET_TEST_SECRET", "leak");
        let out = dir.join("env.txt");
        let script = format!(
            "{{ env; echo core=$(ulimit -c); echo files=$(ulimit -n); echo cwd=$(pwd); }} > {}",
            out.display()
        );
        runner("/bin/true", true)
            .apply(&artifact, Some(&script), "test", None, None)
            .await
            .unwrap();
        let seen = std::fs::read_to_string(out).unwrap();
        assert!(!seen.contains("SUIFLEET_TEST_SECRET"), "{seen}");
        assert!(
            seen.contains("core=0") && seen.contains("files=1024"),
            "{seen}"
        );
        assert!(seen.contains(&format!("HOME={}", dir.display())), "{seen}");
        assert!(seen.contains(&format!("cwd={}", dir.display())), "{seen}");
    }
}
