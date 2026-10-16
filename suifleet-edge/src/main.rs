mod catalog;
mod control;
mod apply;
mod components;
mod config;
mod configuration;
mod deps;
mod firmware;
mod handoff;
mod identity;
mod reconcile;
mod seal;
mod recipe;
mod protocol;
#[cfg(test)]
mod protocol_tests;
#[cfg(test)]
mod compat_tests;
mod release;
mod signer_service;
mod state;
mod sui_client;
mod supervisor;
mod walrus_client;

use apply::ApplyRunner;
use config::Config;
use state::{Journal, Outcome};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};
use sui_client::{DeploymentCreatedEvent, SuiClient};
use tracing::{info, warn};
use walrus_client::WalrusClient;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() == 3 && args[1] == "--derive-address" {
        println!(
            "{}",
            identity::address(&identity::load(Path::new(&args[2]))?)
        );
        return Ok(());
    }
    if args.len() == 3 && args[1] == "--provision" {
        println!("{}", identity::provision(Path::new(&args[2]))?);
        return Ok(());
    }
    if args.len() == 3 && args[1] == "--enroll" {
        return enroll(Path::new(&args[2])).await;
    }
    if args.len() == 4 && args[1] == "--verify-release" {
        let verified = release::verify(Path::new(&args[2]), Path::new(&args[3]))?;
        println!(
            "Verified {} {} ({}, commit {}) signed by {}",
            verified.name, verified.version, verified.target, verified.commit, verified.signer
        );
        return Ok(());
    }
    if args.len() == 3 && args[1] == "--key-holder" {
        // Root provisioning: who owns device.key under this configuration (SF-053).
        println!("{}", signer_service::key_holder(Path::new(&args[2]))?);
        return Ok(());
    }
    if args.len() == 3 && args[1] == "--record-main-process" {
        // Root ExecStartPost= of the daemon unit, for the signing service (SF-053).
        return signer_service::record_main_process(Path::new(&args[2]));
    }
    if args.len() == 3 && args[1] == "--self-check" {
        return self_check(Path::new(&args[2]));
    }
    // The service manager starts the platform's base binary with --launch; it
    // execs a staged or confirmed daemon update, or continues as this binary.
    if args.len() == 2 && args[1] == "--launch" {
        let path = std::env::var("EDGE_CONFIG").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("/etc/suifleet/config.toml"));
        match Config::load(&path) {
            Ok(cfg) => handoff::launch(&cfg.daemon_update, &cfg.runtime.state_dir)?,
            Err(error) => eprintln!("suifleet launcher: configuration unreadable ({error:#}); starting this daemon"),
        }
    }
    let launched = args.len() == 2 && args[1] == "--launch";
    let check_config = args.len() == 3 && args[1] == "--check-config";
    let helper = args.len() == 3 && args[1] == "--firmware-helper";
    let signer_service = args.len() == 3 && args[1] == "--signer-service";
    anyhow::ensure!(
        args.len() == 1 || launched || check_config || helper || signer_service,
        "usage: suifleet [--launch | --provision KEY_PATH | --derive-address KEY_PATH | --enroll ENROLL_TOML | --check-config CONFIG_PATH | --self-check CONFIG_PATH | --firmware-helper CONFIG | --signer-service SIGNER_TOML | --record-main-process PATH | --key-holder CONFIG_PATH | --verify-release BUNDLE_DIR TRUST_FILE]"
    );
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "suifleet=info".into()),
        )
        .init();
    if signer_service {
        // Runs as its own user, holding the device key (SF-053).
        return signer_service::serve(Path::new(&args[2]));
    }
    if helper {
        // Root-owned, path-activated: the only privileged operation.
        let helper = config::FirmwareHelperConfig::load(Path::new(&args[2]))?;
        firmware::run_helper(&helper).await?;
        return Ok(());
    }
    let path = if check_config {
        PathBuf::from(&args[2])
    } else {
        std::env::var("EDGE_CONFIG")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("/etc/suifleet/config.toml"))
    };
    let cfg = Config::load(&path)?;
    if !check_config && cfg.device.signer.as_ref().is_some_and(|signer| signer.kind == "service") {
        // Component processes run as this user: with the key in the signing
        // service, keep them from ptracing the daemon (and so signing as it)
        // or taking its descriptors. execve restores the default for each
        // component.
        // SAFETY: plain prctl on this process.
        anyhow::ensure!(unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) } == 0, "cannot make the daemon non-dumpable");
    }
    let sui = SuiClient::load(cfg.sui.clone(), identity::signer(&cfg.device)?)?;
    sui.validate_deployment().await?;
    if check_config {
        println!("Verified deployment configuration");
        return Ok(());
    }
    let binding = format!(
        "{}/{}/{}/{}",
        sui.manifest().chain_identifier,
        sui.origin(),
        cfg.device.device_object_id,
        sui.address()
    );
    let mut journal = Journal::open(&cfg.runtime.state_dir, binding)?;
    // Before anything else reads the journal: a firmware update that may still
    // finish, or already did, must not be settled as Failed.
    let spool = cfg.apply.firmware_spool.clone();
    journal.settle_interrupted(|id, outcome| match &outcome.firmware_request {
        _ if outcome.daemon_handoff.is_some() => true, // Resolved by the handoff below.
        Some(request) => firmware::recover(&spool, request).unwrap_or_else(|error| {
            warn!(deployment = id, %error, "Cannot inspect firmware spool; keeping outcome pending");
            true
        }),
        None => false,
    })?;
    // A daemon update staged by an earlier run: applied, rolled back, or (when
    // this process is its candidate) confirmed once startup completes.
    let handoff_dir = handoff::install_dir(&cfg.daemon_update, &cfg.runtime.state_dir);
    let mut confirm_after_startup = None;
    if cfg.daemon_update.enabled {
        let candidate = std::env::var(handoff::CANDIDATE_ENV).ok();
        match handoff::resolve(&handoff_dir, candidate.as_deref())? {
            handoff::Resolution::None => {}
            handoff::Resolution::ConfirmAfterStartup(update) => confirm_after_startup = Some(update),
            handoff::Resolution::Applied(update) => settle_handoff(&mut journal, &handoff_dir, &update, Ok(()))?,
            handoff::Resolution::Failed(update, reason) => {
                warn!(deployment = update.deployment, version = update.version, %reason, "Daemon update rolled back");
                settle_handoff(&mut journal, &handoff_dir, &update, Err(anyhow::anyhow!(reason)))?
            }
        }
    }
    let walrus = WalrusClient::new(&cfg.walrus);
    // The artifact cache holds at most the in-flight artifact; anything left
    // by an earlier run (e.g. a crash mid-download) is unused.
    let freed = walrus_client::collect_garbage(&cfg.runtime.state_dir.join("artifacts"))?;
    if freed > 0 {
        info!(
            bytes = freed,
            "Removed leftover artifacts from a previous run"
        );
    }
    let runner = ApplyRunner::new(cfg.apply.clone());
    let store = components::Store::open(&cfg.runtime.state_dir, &cfg.components);
    // Committed components start again on every daemon start (bootstrap/reboot).
    let mut supervisor = supervisor::Supervisor::open(
        &cfg.runtime.state_dir,
        supervisor::Settings {
            startup: Duration::from_secs(cfg.runtime.component_startup_seconds),
            ready_timeout: Duration::from_secs(cfg.runtime.component_ready_timeout_seconds),
            stop_timeout: Duration::from_secs(cfg.runtime.component_stop_timeout_seconds),
            max_restarts: cfg.runtime.component_max_restarts,
            max_memory_bytes: cfg.apply.max_memory_bytes,
            overlay_dir: cfg.components.configuration_overlay_dir.clone(),
        },
    )?;
    supervisor.configure_recovery(cfg.components.recovery_commands.clone());
    let configs = configuration::Store::open(&cfg.runtime.state_dir, &cfg.components.configuration_overlay_dir)?;
    // The daemon is ready before its components are: bootstrap waits up to a
    // readiness timeout per component, which would otherwise exceed the
    // unit's start deadline and restart the daemon into the same bootstrap.
    #[cfg(feature = "systemd")]
    tokio::spawn(async {
        loop {
            sd_notify::notify(false, &[sd_notify::NotifyState::Watchdog]).ok();
            tokio::time::sleep(Duration::from_secs(30)).await;
        }
    });
    #[cfg(feature = "systemd")]
    sd_notify::notify(false, &[sd_notify::NotifyState::Ready]).ok();
    // A new daemon is confirmed only after this complete local startup
    // (configuration, identity, chain validation, journal and supervisor).
    if let Some(update) = confirm_after_startup.take() {
        handoff::confirm(&handoff_dir, &update)?;
        info!(deployment = update.deployment, version = update.version, "Daemon update confirmed");
        settle_handoff(&mut journal, &handoff_dir, &update, Ok(()))?;
    }
    let mut restart_for_handoff = false;
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    // A stop during bootstrap still stops whatever it already started.
    let stopping = tokio::select! {
        _ = supervisor.bootstrap() => false,
        _ = &mut shutdown => { info!("Stopping during component bootstrap"); true }
    };
    if !stopping {
        info!(
            address = sui.address(),
            "Daemon started with durable deployment polling"
        );
        loop {
            let cycle = poll(&cfg, &sui, &walrus, &runner, store.as_ref(), &configs, &mut supervisor, &mut journal);
            tokio::select! {
                result = cycle => match result {
                    Err(error) if error.downcast_ref::<handoff::Requested>().is_some() => {
                        info!("Daemon update staged; stopping components and restarting into it");
                        restart_for_handoff = true;
                        break;
                    }
                    Err(error) => warn!(%error, "Sync failed; retrying without advancing cursor"),
                    Ok(()) => {}
                },
                _ = &mut shutdown => { info!("Stopping; interrupted apply requires inspection on restart"); break; }
            }
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(cfg.runtime.poll_seconds)) => {}
                _ = &mut shutdown => break,
            }
        }
    }
    // Components stop with the daemon (shutdown command, then the whole group).
    supervisor.stop_all().await;
    if restart_for_handoff {
        std::process::exit(handoff::RESTART_EXIT);
    }
    Ok(())
}

/// Record a resolved daemon update's outcome, then drop its handoff record.
fn settle_handoff(journal: &mut Journal, dir: &Path, update: &handoff::Handoff, result: anyhow::Result<()>) -> anyhow::Result<()> {
    let outcome = journal.state.deployments.entry(update.deployment.clone()).or_default();
    if outcome.status.is_none() {
        record_result(outcome, result);
        outcome.daemon_handoff = None;
        journal.save()?;
    }
    handoff::finish(dir, update)
}

/// `suifleet --self-check CONFIG`: offline proof that this binary accepts the
/// device's configuration, manifest, identity and journal. A daemon update is
/// preflighted with it before the running daemon hands over.
fn self_check(path: &Path) -> anyhow::Result<()> {
    let cfg = Config::load(path)?;
    let sui = SuiClient::load(cfg.sui.clone(), identity::signer(&cfg.device)?)?;
    let binding = format!(
        "{}/{}/{}/{}",
        sui.manifest().chain_identifier,
        sui.origin(),
        cfg.device.device_object_id,
        sui.address()
    );
    state::check(&cfg.runtime.state_dir, &binding)?;
    println!("suifleet {} accepts this configuration, identity and journal", env!("CARGO_PKG_VERSION"));
    Ok(())
}

/// Enroll this device under an operator's enrollment policy (SF-021) and
/// print the IDs its daemon configuration needs.
async fn enroll(path: &Path) -> anyhow::Result<()> {
    let (enroll, sui_config, device) = config::EnrollConfig::load(path)?;
    let claim = identity::Ed25519File(identity::load(&enroll.claim_key_path)?);
    let sui = SuiClient::load(sui_config, identity::signer(&device)?)?;
    sui.validate_deployment().await?;
    let arch = enroll.arch.clone().unwrap_or_else(|| std::env::consts::ARCH.to_owned());
    let enrolled = sui.enroll(&enroll.policy_id, &enroll.name, &arch, &claim).await?;
    println!("{}", serde_json::json!({
        "digest": enrolled.digest,
        "device_address": enrolled.device_address,
        "device_object_id": enrolled.device_object_id,
        "device_cap_id": enrolled.device_cap_id,
        "owner_cap_id": enrolled.owner_cap_id,
    }));
    eprintln!("Enrolled. Daemon config:\n[device]\ndevice_object_id = \"{}\"\ndevice_cap_id = \"{}\"",
              enrolled.device_object_id, enrolled.device_cap_id);
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn poll(
    cfg: &Config,
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    store: Option<&components::Store>,
    configs: &configuration::Store,
    supervisor: &mut supervisor::Supervisor,
    journal: &mut Journal,
) -> anyhow::Result<()> {
    supervisor.recover_broken(runner, |name| journal.state.components.get(name).is_some_and(|c| c.confirmed)).await?;
    // A recovery receipt and the retained identity reconcile even when the
    // daemon stopped between supervisor commit and journal save.
    for (name, deployment) in supervisor.recovered() {
        if !supervisor.restored(name, deployment) { continue; }
        // Later updates build on the restored version's configuration, not
        // on the failed one's.
        configs.restore(name, supervisor.configuration(name)?.as_ref())?;
        if let Some(previous) = journal.state.last_known_good.get(name).filter(|c| &c.deployment_id == deployment).cloned() {
            journal.state.current.insert(name.clone(), deployment.clone());
            journal.state.components.insert(name.clone(), previous);
        }
    }
    journal.save()?;
    sui.validate_deployment().await?;
    sui.validate_cap(&cfg.device.device_cap_id, &cfg.device.device_object_id)
        .await?;
    if !journal.state.initialized {
        if !cfg.runtime.replay_history {
            journal.state.cursor = sui.latest_cursor().await?;
        }
        journal.state.initialized = true;
        journal.save()?;
    }
    if cfg.apply.mode == config::ApplyMode::PrivilegedHelper {
        collect_firmware_results(cfg, journal)?;
    }
    settle_configuration(configs, journal, supervisor)?;
    // Reports remain queued across network failures and daemon restarts.
    let pending: Vec<_> = journal
        .state
        .deployments
        .iter()
        .filter(|(_, outcome)| !outcome.reported && outcome.status.is_some())
        .map(|(id, outcome)| (id.clone(), outcome.status.unwrap()))
        .collect();
    for (id, status) in pending {
        match sui
            .report_status(&id, &cfg.device.device_cap_id, status)
            .await
        {
            Ok(_) => {
                journal.state.deployments.get_mut(&id).unwrap().reported = true;
                journal.save()?;
            }
            Err(error) => warn!(%id, %error, "Status report remains queued"),
        }
    }
    loop {
        let page = sui.deployments(&journal.state.cursor).await?;
        for (event, cursor) in page.events {
            if !journal.state.deployments.contains_key(&event.deployment_id)
                && sui
                    .authorized_target(&event, &cfg.device.device_object_id)
                    .await?
            {
                // Deletion is not a cancellation protocol, but deleted records must not trigger apply.
                let Some(record) = sui.optional_object(&event.deployment_id).await? else {
                    journal.state.cursor = Some(cursor);
                    journal.save()?;
                    continue;
                };
                // Desired state owns this record: reconciliation applies it
                // only while it is the current revision's choice.
                if sui.is_managed(&event.deployment_id).await? {
                    info!(deployment = event.deployment_id, "Managed by desired state; left to reconciliation");
                } else {
                    execute(cfg, sui, walrus, runner, store, configs, supervisor, journal, &event, &record).await?;
                }
            }
            journal.state.cursor = Some(cursor);
            journal.save()?;
        }
        // Resume after everything this page scanned, events or not.
        let progressed = journal.state.cursor.as_ref() != Some(&page.end);
        if progressed {
            journal.state.cursor = Some(page.end);
            journal.save()?;
        }
        if !page.has_more {
            break;
        }
        anyhow::ensure!(progressed, "event stream claims more data without advancing");
    }
    // Deferred one-shot events survive cursor advancement and restarts.
    for id in journal.state.deferred.keys().cloned().collect::<Vec<_>>() {
        if journal.state.deferred[&id].terminal || journal.state.deployments.contains_key(&id)
            || journal.state.managed.values().any(|managed| managed == &id) {
            // Reconciliation owns managed notices, including their durable timer.
            continue;
        }
        let Some((event, record)) = sui.deployment(&id).await? else {
            journal.state.deferred.remove(&id);
            journal.save()?;
            continue;
        };
        if sui.is_managed(&id).await? {
            journal.state.deferred.remove(&id);
            journal.save()?;
        } else if sui.authorized_target(&event, &cfg.device.device_object_id).await? {
            execute(cfg, sui, walrus, runner, store, configs, supervisor, journal, &event, &record).await?;
        }
    }
    reconcile(cfg, sui, walrus, runner, store, configs, supervisor, journal).await
}

/// Commit, keep or discard the configuration each interrupted or finished
/// install staged, by what became of it: the version the supervisor
/// committed or a confirmed install keeps its configuration; a privileged
/// update with an unknown outcome keeps it staged.
fn settle_configuration(
    configs: &configuration::Store,
    journal: &mut Journal,
    supervisor: &supervisor::Supervisor,
) -> anyhow::Result<()> {
    // A deferral matters only while its deployment still awaits admission.
    let state = &mut journal.state;
    let before = state.configuration_deferrals.len();
    state.configuration_deferrals.retain(|id, _| {
        state.deferred.get(id).is_some_and(|d| !d.terminal) && !state.deployments.contains_key(id)
    });
    if state.configuration_deferrals.len() != before {
        journal.save()?;
    }
    configs.settle(|name, deployment| {
        let confirmed = journal
            .state
            .components
            .get(name)
            .is_some_and(|c| c.confirmed && c.deployment_id == deployment);
        if confirmed || supervisor.committed(name) == Some(deployment) {
            configuration::Settled::Committed
        } else if journal.state.deployments.get(deployment).is_some_and(|o| o.status.is_none()) {
            configuration::Settled::Pending
        } else {
            configuration::Settled::Abandoned
        }
    })
}

/// Apply one authorized deployment, journaling intent first, and record its
/// outcome (reported on a later poll). Returns the recorded status.
#[allow(clippy::too_many_arguments)]
async fn execute(
    cfg: &Config,
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    store: Option<&components::Store>,
    configs: &configuration::Store,
    supervisor: &mut supervisor::Supervisor,
    journal: &mut Journal,
    event: &DeploymentCreatedEvent,
    record: &sui_client::Object,
) -> anyhow::Result<Option<u8>> {
    anyhow::ensure!(
        sui_client::same_type(
            &record.object_type,
            &sui.move_type(protocol::deployment_manager::DeploymentRecord::MOVE_TYPE)
        ),
        "wrong deployment type"
    );
    if !admit(sui, journal, &event.deployment_id).await? {
        return Ok(None);
    }
    let resolved = catalog::resolve(sui, event, &record.fields).await;
    if let Err(error) = &resolved {
        let mut outcome = Outcome::default();
        record_result(&mut outcome, Err(anyhow::anyhow!("{error:#}")));
        warn!(deployment = event.deployment_id, error = ?outcome.error, "Deployment schema rejected");
        let status = outcome.status;
        journal.state.deployments.insert(event.deployment_id.clone(), outcome);
        journal.save()?;
        return Ok(status);
    }
    let resolved = resolved?;
    if cfg.daemon_update.enabled && event.component_name == cfg.daemon_update.component {
        return stage_daemon_update(cfg, sui, walrus, store, journal, &resolved).await;
    }
    let directory = cfg
        .runtime
        .state_dir
        .join("artifacts")
        .join(hex::encode(identity::hash(event.deployment_id.as_bytes())));
    std::fs::create_dir_all(&directory)?;
    let result =
        handle_deployment(cfg, sui, walrus, runner, store, configs, supervisor, journal, &resolved, &directory).await;
    if matches!(&result, Err(error) if error.downcast_ref::<control::NotAdmitted>().is_some()) {
        let _ = std::fs::remove_dir_all(&directory);
        return Ok(None);
    }
    let outcome = journal.state.deployments.entry(event.deployment_id.clone()).or_default();
    record_result(outcome, result);
    info!(deployment = event.deployment_id, status = outcome.status, error = ?outcome.error, "Deployment finished");
    let status = outcome.status;
    journal.save()?;
    if status.is_some() {
        firmware::acknowledge(&cfg.apply.firmware_spool, &firmware::request_id(&event.deployment_id));
    }
    let _ = std::fs::remove_dir_all(&directory);
    Ok(status)
}

/// A deployment of the configured daemon component: verify and stage the new
/// binary, journal the deployment as pending, and ask for a restart. Failures
/// before the handoff is recorded are ordinary Failed outcomes; a deployment no
/// longer admitted after the downloads stays deferred.
async fn stage_daemon_update(
    cfg: &Config,
    sui: &SuiClient,
    walrus: &WalrusClient,
    store: Option<&components::Store>,
    journal: &mut Journal,
    resolved: &catalog::Resolved,
) -> anyhow::Result<Option<u8>> {
    let event = &resolved.event;
    let directory = cfg.runtime.state_dir.join("artifacts").join(hex::encode(identity::hash(event.deployment_id.as_bytes())));
    std::fs::create_dir_all(&directory)?;
    let staged = async {
        let bytes = components::recipe(walrus, store, &event.recipe_blob_id, &resolved.recipe_sha256).await?;
        let recipe: recipe::Recipe = serde_json::from_slice(&bytes)?;
        let dependencies = recipe.validate(event, false)?;
        let lifecycle = &recipe.lifecycle;
        anyhow::ensure!(
            dependencies.is_empty() && lifecycle.install.is_none() && lifecycle.run.is_none() && lifecycle.shutdown.is_none(),
            "a daemon update recipe carries only the daemon binary (no lifecycle commands or dependencies)"
        );
        let artifact = directory.join("artifact");
        components::artifact(walrus, store, &recipe.artifact_blob_id, &event.artifact_sha256, &artifact).await?;
        // As in handle_deployment: re-read after downloads, before the
        // candidate runs or the daemon restarts. Read failure fails closed.
        match admit(sui, journal, &event.deployment_id).await {
            Ok(true) => {}
            Ok(false) => return Err(control::NotAdmitted.into()),
            Err(error) => {
                warn!(%error, "Admission unavailable; retrying before the daemon update");
                return Err(control::NotAdmitted.into());
            }
        }
        handoff::stage(&cfg.daemon_update, &cfg.runtime.state_dir, &cfg.path, &event.deployment_id, &event.version, &artifact).await
    }
    .await;
    let _ = std::fs::remove_dir_all(&directory);
    if matches!(&staged, Err(error) if error.downcast_ref::<control::NotAdmitted>().is_some()) {
        return Ok(None);
    }
    if staged.is_ok() {
        journal.state.deferred.remove(&event.deployment_id);
    }
    let outcome = journal.state.deployments.entry(event.deployment_id.clone()).or_default();
    match staged {
        Ok(()) => {
            outcome.daemon_handoff = Some(event.version.clone());
            journal.save()?;
            info!(deployment = event.deployment_id, version = event.version, "Daemon update staged");
            Err(handoff::Requested.into())
        }
        Err(error) => {
            record_result(outcome, Err(error));
            warn!(deployment = event.deployment_id, error = ?outcome.error, "Daemon update rejected");
            let status = outcome.status;
            journal.save()?;
            Ok(status)
        }
    }
}

/// Persist notice and deferral independently of execution outcomes. A terminal
/// observation is never forgotten if the device clock subsequently moves back.
async fn admit(sui: &SuiClient, journal: &mut Journal, id: &str) -> anyhow::Result<bool> {
    if journal.state.deferred.get(id).is_some_and(|d| d.terminal) { return Ok(false); }
    // A missing record (including old-package deletion) is never permission.
    anyhow::ensure!(sui.optional_object(id).await?.is_some(), "deployment deleted before admission");
    let control = sui.deployment_control(id).await?;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_millis();
    let now = u64::try_from(now)?;
    // Use corrected UTC for schedule/window checks. Only a deployment's
    // terminal observation survives clock rollback, never a global clock floor.
    // Restart notice if its saved timestamp is ahead of the corrected clock.
    let notified = journal.state.deferred.get(id)
        .filter(|d| d.control == control && d.notified_ms <= now)
        .map(|d| d.notified_ms).unwrap_or(now);
    let reason = control.reason(now, notified)?;
    let entry = control::Deferred {
        control, notified_ms: notified,
        reason: reason.unwrap_or("ready for admission").to_owned(),
        terminal: matches!(reason, Some("cancelled" | "expired")),
    };
    if journal.state.deferred.get(id).is_none_or(|old| old.reason != entry.reason || old.control != entry.control) {
        info!(deployment = id, reason = entry.reason, notice_ms = entry.control.notice_ms,
            "Deployment admission notice (UTC); no disruptive work started");
    }
    journal.state.deferred.insert(id.to_owned(), entry);
    journal.save()?;
    Ok(reason.is_none())
}

/// Move toward the current desired state of every group this device is in
/// (reconcile.rs describes the rules).
#[allow(clippy::too_many_arguments)]
async fn reconcile(
    cfg: &Config,
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    store: Option<&components::Store>,
    configs: &configuration::Store,
    supervisor: &mut supervisor::Supervisor,
    journal: &mut Journal,
) -> anyhow::Result<()> {
    let device = &cfg.device.device_object_id;
    // Groups that ever added this device, scanned from genesis: membership
    // can predate enrollment. Each group object is the authority below.
    loop {
        let page = sui.memberships(&journal.state.membership_cursor).await?;
        for (group, member) in page.events {
            if sui_client::same_id(&member, device) && journal.state.groups.insert(group.clone()) {
                info!(group, "Device was added to a group; following its desired state");
            }
        }
        let progressed = journal.state.membership_cursor.as_ref() != Some(&page.end);
        journal.state.membership_cursor = Some(page.end);
        journal.save()?;
        if !page.has_more {
            break;
        }
        anyhow::ensure!(progressed, "membership stream claims more data without advancing");
    }
    let operator = sui.operator(device).await?;
    let mut groups = Vec::new();
    for group in journal.state.groups.clone() {
        if let Some(desired) = sui.group_desired(&group, &operator, device).await? {
            groups.push(desired);
        }
    }
    let effective = reconcile::effective(&groups);
    let fingerprint = reconcile::fingerprint(&effective);
    let outcome = |journal: &Journal, deployment: &str| {
        journal
            .state
            .deployments
            .iter()
            .find(|(id, _)| sui_client::same_id(id, deployment))
            .map(|(_, outcome)| outcome.clone())
    };
    let changed = journal.state.desired.as_deref() != Some(fingerprint.as_str());
    if changed {
        for (component, sources) in &effective.conflicts {
            warn!(component, ?sources, "Groups disagree on this component; leaving it as installed");
        }
        journal.state.desired = Some(fingerprint.clone());
    }
    for component in reconcile::retired(&journal.state.managed, &effective) {
        info!(component, "No longer desired by any group; retired from management (not uninstalled)");
        journal.state.managed.remove(&component);
    }
    // The step journaled last, read back from the journal on every poll, so a
    // crash at any point (mid-step, or before the failure is acted on) keeps
    // later steps stopped, and a newer desired state seen while it was in
    // flight is planned once it settles. No intent saved means its updater
    // never started.
    if let Some(step) = journal.state.step.clone() {
        match outcome(journal, &step.deployment) {
            Some(o) if o.status != Some(protocol::STATUS_APPLIED) => {
                let settled = o.status.is_some() || o.indeterminate;
                if reconcile::step_holds(&step.desired, &fingerprint, settled) {
                    journal.save()?;
                    return Ok(());
                }
                info!(deployment = step.deployment, "Stopped step settled; planning the newer desired state");
                journal.state.step = None;
            }
            _ => journal.state.step = None,
        }
    }
    let history = |journal: &Journal, deployment: &str| match outcome(journal, deployment) {
        None => reconcile::History::Never,
        Some(o) if o.status == Some(protocol::STATUS_APPLIED) => reconcile::History::Applied,
        Some(_) => reconcile::History::NotApplied,
    };
    let steps = reconcile::plan(&effective, &journal.state.current, |deployment| history(journal, deployment));
    for step in steps {
        let (component, deployment) = match step {
            reconcile::Step::Adopt(component, deployment) => {
                journal.state.current.insert(component.clone(), deployment.clone());
                journal.state.managed.insert(component, deployment);
                continue;
            }
            reconcile::Step::Reselected(component, deployment) => {
                if changed {
                    warn!(component, deployment, current = journal.state.current.get(&component),
                        "Desired deployment already ran here and the component has changed since; a deployment never runs twice, so create a new record for this version. Leaving it as installed");
                }
                continue;
            }
            reconcile::Step::Apply(component, deployment) => (component, deployment),
        };
        let Some((event, record)) = sui.deployment(&deployment).await? else {
            warn!(component, deployment, "Desired deployment was deleted; skipping it");
            continue;
        };
        // Enforced on chain too; never apply another operator's update.
        if !sui_client::same_id(&event.creator, &operator) || event.component_name != component {
            warn!(component, deployment, "Desired deployment is not this operator's component; skipping it");
            continue;
        }
        info!(component, deployment, version = event.version, "Reconciling to desired state");
        journal.state.step = Some(state::Step {
            deployment: deployment.clone(),
            desired: fingerprint.clone(),
        });
        journal.state.managed.insert(component.clone(), deployment.clone());
        journal.save()?;
        let status = execute(cfg, sui, walrus, runner, store, configs, supervisor, journal, &event, &record).await?;
        if status != Some(protocol::STATUS_APPLIED) {
            if status.is_none() {
                info!(component, deployment, "Plan deferred; admission reconsidered on the next poll");
            } else {
                warn!(component, deployment, "Plan stopped; not retried until the desired state changes");
            }
            return Ok(());
        }
        journal.state.step = None;
        journal.save()?;
    }
    journal.save()
}

/// One component the deployment installs, with its verified artifact.
struct Install {
    component: state::Component,
    name: String,
    blob: String,
    artifact: PathBuf,
    install: Option<String>,
    supervised: Option<supervisor::Lifecycle>,
    root: bool,
    configuration: protocol::Configuration,
    validate: Option<String>,
    /// Its next configuration, computed and validated before any install.
    record: configuration::Record,
}
impl Install {
    fn context(&self, error: anyhow::Error) -> anyhow::Error {
        if self.root {
            error
        } else {
            error.context(format!("dependency {} {}", self.name, self.component.version))
        }
    }
}

/// Resolve the deployed component's dependencies, fetch and verify every
/// artifact the plan installs, then install dependencies first. Conflicts,
/// cycles and unavailable candidates are rejected before anything changes.
#[allow(clippy::too_many_arguments)]
async fn handle_deployment(
    cfg: &Config,
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    store: Option<&components::Store>,
    configs: &configuration::Store,
    supervisor: &mut supervisor::Supervisor,
    journal: &mut Journal,
    resolved: &catalog::Resolved,
    directory: &Path,
) -> anyhow::Result<()> {
    let config = &cfg.apply;
    let event = &resolved.event;
    // The deployed version can later satisfy dependencies from the cache.
    if let (Some(store), Some(catalog), Some(version)) = (store, &resolved.catalog, &resolved.version) {
        if let Err(error) = store.save_version(&catalog.version_id, version) {
            warn!(%error, "Cannot cache component version");
        }
    }
    let bytes =
        components::recipe(walrus, store, &event.recipe_blob_id, &resolved.recipe_sha256).await?;
    let recipe: recipe::Recipe = serde_json::from_slice(&bytes)?;
    let dependencies = recipe.validate(event, config.allow_recipe_commands)?;
    let root = deps::Root {
        name: event.component_name.clone(),
        version: event.version.clone(),
        dependencies,
    };
    let mut loader = components::Loader::new(
        sui,
        walrus,
        store,
        resolved.catalog.as_ref(),
        config.allow_recipe_commands,
    );
    let installed = components::installed(&journal.state.components);
    let plan = components::plan(&mut loader, &root, &installed).await?;
    if !plan.omitted.is_empty() {
        info!(deployment = event.deployment_id, omitted = ?plan.omitted, "Unavailable soft dependencies omitted");
    }
    let mut installs = Vec::new();
    for (index, planned) in plan.order.iter().filter(|p| p.install()).enumerate() {
        let root = planned.source == deps::Source::Root;
        let component = |version_id, recipe_sha256: &str, artifact_sha256: &str| state::Component {
            version: planned.version.clone(),
            deployment_id: event.deployment_id.clone(),
            catalog_id: resolved.catalog.as_ref().map(|c| c.catalog_id.clone()),
            version_id,
            recipe_sha256: recipe_sha256.to_owned(),
            artifact_sha256: artifact_sha256.to_owned(),
            dependencies: planned.dependencies.clone(),
            confirmed: false,
        };
        let (component, blob, install, supervised, configuration, validate) = if root {
            (
                component(
                    resolved.catalog.as_ref().map(|c| c.version_id.clone()),
                    &resolved.recipe_sha256,
                    &event.artifact_sha256,
                ),
                recipe.artifact_blob_id.clone(),
                recipe.lifecycle.install.clone(),
                supervisor::Lifecycle::of(&recipe.lifecycle)?,
                recipe.configuration.clone(),
                recipe.lifecycle.validate.clone(),
            )
        } else {
            let loaded = &loader.loaded[&(planned.name.clone(), planned.version.clone())];
            // The privileged helper only ever receives the deployed artifact;
            // a supervised dependency never goes to it.
            anyhow::ensure!(
                config.mode == config::ApplyMode::Direct || loaded.install.is_some() || loaded.supervised.is_some(),
                "dependency {} {} has no install command; the firmware helper applies only the deployed component",
                planned.name,
                planned.version
            );
            (
                component(Some(loaded.version_id.clone()), &loaded.recipe_sha256, &loaded.artifact_sha256),
                loaded.artifact_blob_id.clone(),
                loaded.install.clone(),
                loaded.supervised.clone(),
                loaded.configuration.clone(),
                loaded.validate.clone(),
            )
        };
        installs.push(Install {
            artifact: if root {
                directory.join("artifact")
            } else {
                directory.join(format!("dependency-{index}"))
            },
            name: planned.name.clone(),
            blob,
            component,
            install,
            supervised,
            root,
            configuration,
            validate,
            record: configuration::Record::default(),
        });
    }
    // Every artifact is verified before the first install starts.
    for install in &installs {
        components::artifact(walrus, store, &install.blob, &install.component.artifact_sha256, &install.artifact)
            .await
            .map_err(|e| install.context(e))?;
    }
    // So is every configuration: computed from the committed one, provisioned
    // and accepted by the component's validate command, or nothing changes.
    if !prepare_configurations(cfg, configs, supervisor, journal, &event.deployment_id, &mut installs, directory).await? {
        return Err(control::NotAdmitted.into());
    }
    // Re-read after downloads: a pause/cancel/window closing during fetch
    // cannot pass a stale initial check. Read failure fails closed before intent.
    match admit(sui, journal, &event.deployment_id).await {
        Ok(true) => {},
        Ok(false) => return Err(control::NotAdmitted.into()),
        Err(error) => {
            warn!(%error, "Admission unavailable; retrying before apply");
            return Err(control::NotAdmitted.into());
        }
    }
    journal.state.current.insert(event.component_name.clone(), event.deployment_id.clone());
    journal.state.deployments.insert(event.deployment_id.clone(), Outcome::default());
    journal.state.deferred.remove(&event.deployment_id);
    journal.save()?;
    info!(deployment = event.deployment_id, "Apply admitted; subsequent control changes do not interrupt this plan");
    for (index, install) in installs.into_iter().enumerate() {
        supervisor.begin_install(&install.name)?;
        // Staged before the install starts, committed once it is confirmed
        // (settle_configuration covers an interruption in between).
        configs.stage(&install.name, &install.record)?;
        // Recorded unconfirmed first: an interrupted install is never reused.
        // A dependency install changes what that component runs as well.
        let previous = journal.state.components.get(&install.name).cloned().filter(|c| c.confirmed);
        if let Some(previous) = &previous {
            journal.state.last_known_good.insert(install.name.clone(), previous.clone());
        }
        journal
            .state
            .components
            .insert(install.name.clone(), install.component.clone());
        journal
            .state
            .current
            .insert(install.name.clone(), event.deployment_id.clone());
        // Only the actual helper handoff has a helper receipt. Recipe and
        // dependency commands remain direct, even under helper-mode config.
        journal.state.deployments.get_mut(&event.deployment_id).unwrap().firmware_request =
            (install.root && install.supervised.is_none() && install.install.is_none()
                && config.mode == config::ApplyMode::PrivilegedHelper)
                .then(|| firmware::request_id(&event.deployment_id));
        journal.save()?;
        let request = if install.root {
            firmware::request_id(&event.deployment_id)
        } else {
            firmware::request_id(&format!("{}/{}", event.deployment_id, install.name))
        };
        // A run command makes it a supervised component: installed into its
        // version workspace and confirmed only once ready (dependencies,
        // planned first, are therefore ready before their dependents start).
        let result = match install.supervised.clone() {
            Some(lifecycle) => {
                let after = install.component.dependencies.keys().cloned().collect();
                let spec = supervisor::Spec::new(&install.name, &install.component.version, &event.deployment_id, lifecycle, after);
                supervisor
                    .deploy(spec, &install.artifact, install.install.as_deref(), runner, &request, &install.record)
                    .await
            }
            // Installed while a supervised version of the same name keeps
            // running; once it succeeds, that version is stopped and
            // uncommitted, or the deployment fails.
            None => {
                let applied = match install_configuration(configs, &install, directory, index) {
                    Ok(file) => match supervisor.component_log(&install.name) {
                        Ok(output) => {
                            runner
                                .apply(&install.artifact, install.install.as_deref(), &request, Some(&file), Some(output))
                                .await
                        }
                        Err(error) => Err(error),
                    },
                    Err(error) => Err(error),
                };
                match applied {
                    Ok(()) if supervisor.supervises(&install.name) => supervisor
                        .retire(&install.name)
                        .await
                        .map_err(|error| error.context("installed, but cannot stop the supervised version it replaces")),
                    result => result,
                }
            }
        };
        if let Err(error) = result {
            if let Some(previous) = previous.filter(|c| error.downcast_ref::<supervisor::Recovered>().is_some() && supervisor.restored(&install.name, &c.deployment_id)) {
                journal.state.current.insert(install.name.clone(), previous.deployment_id.clone());
                journal.state.components.insert(install.name.clone(), previous);
                journal.save()?;
            }
            return Err(if install.root {
                error
            } else {
                error.context(format!(
                    "dependency {} {} install failed",
                    install.name, install.component.version
                ))
            });
        }
        journal.state.components.get_mut(&install.name).unwrap().confirmed = true;
        journal.save()?;
        configs.commit(&install.name, &event.deployment_id)?;
        info!(deployment = event.deployment_id, component = install.name, version = install.component.version, "Component installed");
    }
    if let Some(store) = store {
        let keep = journal
            .state
            .components
            .values()
            .map(|c| c.artifact_sha256.clone())
            .collect();
        if let Err(error) = store.evict(&keep) {
            warn!(%error, "Cannot evict cached artifacts");
        }
    }
    Ok(())
}

/// Compute each install's next configuration, check that its confidential
/// values are provisioned, and run its validate command against it. Returns
/// false when a component defers the update (retried on a later poll, up to
/// `configuration_defer_max_seconds`); an error rejects the deployment
/// before anything changed.
async fn prepare_configurations(
    cfg: &Config,
    configs: &configuration::Store,
    supervisor: &supervisor::Supervisor,
    journal: &mut Journal,
    deployment: &str,
    installs: &mut [Install],
    directory: &Path,
) -> anyhow::Result<bool> {
    let timeout = Duration::from_secs(cfg.apply.timeout_seconds);
    for (index, install) in installs.iter_mut().enumerate() {
        let checked = async {
            let previous = configs.committed(&install.name)?;
            let configuration = &install.configuration;
            let record = configuration::Record {
                deployment: deployment.to_owned(),
                version: install.component.version.clone(),
                defaults: configuration.defaults.clone().unwrap_or_default(),
                effective: configuration::next(previous.as_ref().map(|r| &r.effective), configuration)?,
                confidential: configuration.confidential.clone().unwrap_or_default(),
            };
            let overlay = configuration::overlay(&configs.overlay_dir, &install.name)?;
            let materialized = configuration::materialize(&record, &overlay)?;
            install.record = record;
            let Some(command) = &install.validate else {
                return Ok(true);
            };
            let file = directory.join(format!("validate-{index}.json"));
            configuration::write_private(&file, &materialized)?;
            let verdict = supervisor
                .validate(&install.name, &install.component.version, command, &install.artifact, &file, timeout)
                .await;
            let _ = std::fs::remove_file(&file);
            verdict
        }
        .await
        .map_err(|e| install.context(e.context("configuration")))?;
        if checked {
            continue;
        }
        let now = u64::try_from(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_millis())?;
        let since = *journal.state.configuration_deferrals.entry(deployment.to_owned()).or_insert(now);
        let limit = cfg.components.configuration_defer_max_seconds;
        if now.saturating_sub(since) < limit.saturating_mul(1000) {
            journal.save()?;
            info!(deployment, component = install.name, "Component deferred its configuration update; retrying on a later poll");
            return Ok(false);
        }
        journal.state.configuration_deferrals.remove(deployment);
        journal.save()?;
        return Err(install.context(anyhow::anyhow!(
            "configuration update still deferred by the component after {limit}s"
        )));
    }
    if journal.state.configuration_deferrals.remove(deployment).is_some() {
        journal.save()?;
    }
    Ok(true)
}

/// The materialized configuration file an install-only component's install
/// script receives as `{configuration}`; removed with the deployment's
/// directory.
fn install_configuration(
    configs: &configuration::Store,
    install: &Install,
    directory: &Path,
    index: usize,
) -> anyhow::Result<PathBuf> {
    let overlay = configuration::overlay(&configs.overlay_dir, &install.name)?;
    let file = directory.join(format!("configuration-{index}.json"));
    configuration::write_private(&file, &configuration::materialize(&install.record, &overlay)?)?;
    Ok(file)
}

/// Applied/Failed, or no status when a configured command's outcome is
/// unknown: like a crash mid-update, that is neither reported nor re-applied.
fn record_result(outcome: &mut Outcome, result: anyhow::Result<()>) {
    outcome.indeterminate = matches!(&result, Err(error) if error.downcast_ref::<firmware::Indeterminate>().is_some());
    outcome.status = match &result {
        Ok(()) => Some(protocol::STATUS_APPLIED),
        Err(_) if outcome.indeterminate => None,
        Err(_) => Some(protocol::STATUS_FAILED),
    };
    outcome.error = result.err().map(|error| format!("{error:#}"));
}

/// Pick up firmware-helper results for deployments still without a status:
/// updates that outlived the daemon's wait or a daemon restart.
fn collect_firmware_results(cfg: &Config, journal: &mut Journal) -> anyhow::Result<()> {
    let spool = &cfg.apply.firmware_spool;
    let waiting: Vec<(String, String)> = journal
        .state
        .deployments
        .iter()
        .filter(|(_, outcome)| outcome.status.is_none())
        .filter_map(|(id, outcome)| Some((id.clone(), outcome.firmware_request.clone()?)))
        .collect();
    for (id, request) in waiting {
        let Some(result) = firmware::collect(spool, &request)? else {
            continue;
        };
        let outcome = journal.state.deployments.get_mut(&id).unwrap();
        record_result(outcome, result);
        info!(deployment = id, status = outcome.status, error = ?outcome.error, "Late firmware result collected");
        if outcome.status == Some(protocol::STATUS_APPLIED) {
            journal.confirm_components(&id);
        }
        journal.save()?;
        firmware::acknowledge(spool, &request);
    }
    Ok(())
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut terminate = signal(SignalKind::terminate()).expect("SIGTERM handler");
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = terminate.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
