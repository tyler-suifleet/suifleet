mod catalog;
mod apply;
mod components;
mod config;
mod deps;
mod firmware;
mod identity;
mod recipe;
mod protocol;
#[cfg(test)]
mod protocol_tests;
mod release;
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
use sui_client::SuiClient;
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
    let check_config = args.len() == 3 && args[1] == "--check-config";
    let helper = args.len() == 3 && args[1] == "--firmware-helper";
    anyhow::ensure!(
        args.len() == 1 || check_config || helper,
        "usage: suifleet [--provision KEY_PATH | --derive-address KEY_PATH | --enroll ENROLL_TOML | --check-config CONFIG_PATH | --firmware-helper CONFIG | --verify-release BUNDLE_DIR TRUST_FILE]"
    );
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "suifleet=info".into()),
        )
        .init();
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
        Some(request) => firmware::recover(&spool, request).unwrap_or_else(|error| {
            warn!(deployment = id, %error, "Cannot inspect firmware spool; keeping outcome pending");
            true
        }),
        None => false,
    })?;
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
        },
    )?;
    supervisor.bootstrap().await;
    #[cfg(feature = "systemd")]
    tokio::spawn(async {
        loop {
            sd_notify::notify(false, &[sd_notify::NotifyState::Watchdog]).ok();
            tokio::time::sleep(Duration::from_secs(30)).await;
        }
    });
    #[cfg(feature = "systemd")]
    sd_notify::notify(false, &[sd_notify::NotifyState::Ready]).ok();
    info!(
        address = sui.address(),
        "Daemon started with durable deployment polling"
    );
    let shutdown = shutdown_signal();
    tokio::pin!(shutdown);
    loop {
        let cycle = poll(&cfg, &sui, &walrus, &runner, store.as_ref(), &mut supervisor, &mut journal);
        tokio::select! {
            result = cycle => { if let Err(error) = result { warn!(%error, "Sync failed; retrying without advancing cursor"); } }
            _ = &mut shutdown => { info!("Stopping; interrupted apply requires inspection on restart"); break; }
        }
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(cfg.runtime.poll_seconds)) => {}
            _ = &mut shutdown => break,
        }
    }
    // Components stop with the daemon (shutdown command, then the whole group).
    supervisor.stop_all().await;
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

async fn poll(
    cfg: &Config,
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    store: Option<&components::Store>,
    supervisor: &mut supervisor::Supervisor,
    journal: &mut Journal,
) -> anyhow::Result<()> {
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
                anyhow::ensure!(
                    sui_client::same_type(
                        &record.object_type,
                        &sui.move_type(protocol::deployment_manager::DeploymentRecord::MOVE_TYPE)
                    ),
                    "wrong deployment type"
                );
                let resolved = catalog::resolve(sui, &event, &record.fields).await;
                if let Err(error) = &resolved {
                    let mut outcome = Outcome::default();
                    record_result(&mut outcome, Err(anyhow::anyhow!("{error:#}")));
                    warn!(deployment = event.deployment_id, error = ?outcome.error, "Deployment schema rejected");
                    journal
                        .state
                        .deployments
                        .insert(event.deployment_id.clone(), outcome);
                    journal.state.cursor = Some(cursor);
                    journal.save()?;
                    continue;
                }
                let resolved = resolved?;
                let directory = cfg
                    .runtime
                    .state_dir
                    .join("artifacts")
                    .join(hex::encode(identity::hash(event.deployment_id.as_bytes())));
                std::fs::create_dir_all(&directory)?;
                // Save intent before starting work. A crash never silently re-executes an updater.
                journal.state.deployments.insert(
                    event.deployment_id.clone(),
                    Outcome {
                        firmware_request: (cfg.apply.mode == config::ApplyMode::PrivilegedHelper)
                            .then(|| firmware::request_id(&event.deployment_id)),
                        ..Outcome::default()
                    },
                );
                journal.save()?;
                let result = handle_deployment(
                    sui, walrus, runner, &cfg.apply, store, supervisor, journal, &resolved, &directory,
                )
                .await;
                let outcome = journal
                    .state
                    .deployments
                    .get_mut(&event.deployment_id)
                    .unwrap();
                record_result(outcome, result);
                info!(deployment = event.deployment_id, status = outcome.status, error = ?outcome.error, "Deployment finished");
                journal.save()?;
                firmware::acknowledge(
                    &cfg.apply.firmware_spool,
                    &firmware::request_id(&event.deployment_id),
                );
                let _ = std::fs::remove_dir_all(&directory);
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
    Ok(())
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
}

/// Resolve the deployed component's dependencies, fetch and verify every
/// artifact the plan installs, then install dependencies first. Conflicts,
/// cycles and unavailable candidates are rejected before anything changes.
#[allow(clippy::too_many_arguments)]
async fn handle_deployment(
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    config: &config::ApplyConfig,
    store: Option<&components::Store>,
    supervisor: &mut supervisor::Supervisor,
    journal: &mut Journal,
    resolved: &catalog::Resolved,
    directory: &Path,
) -> anyhow::Result<()> {
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
        let (component, blob, install, supervised) = if root {
            (
                component(
                    resolved.catalog.as_ref().map(|c| c.version_id.clone()),
                    &resolved.recipe_sha256,
                    &event.artifact_sha256,
                ),
                recipe.artifact_blob_id.clone(),
                recipe.lifecycle.install.clone(),
                supervisor::Lifecycle::of(&recipe.lifecycle)?,
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
        });
    }
    // Every artifact is verified before the first install starts.
    for install in &installs {
        components::artifact(walrus, store, &install.blob, &install.component.artifact_sha256, &install.artifact)
            .await
            .map_err(|e| {
                if install.root {
                    e
                } else {
                    e.context(format!("dependency {} {}", install.name, install.component.version))
                }
            })?;
    }
    for install in installs {
        // Recorded unconfirmed first: an interrupted install is never reused.
        journal
            .state
            .components
            .insert(install.name.clone(), install.component.clone());
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
                    .deploy(spec, &install.artifact, install.install.as_deref(), runner, &request)
                    .await
            }
            // Installed while a supervised version of the same name keeps
            // running; once it succeeds, that version is stopped and
            // uncommitted, or the deployment fails.
            None => match runner.apply(&install.artifact, install.install.as_deref(), &request).await {
                Ok(()) if supervisor.supervises(&install.name) => supervisor
                    .retire(&install.name)
                    .await
                    .map_err(|error| error.context("installed, but cannot stop the supervised version it replaces")),
                result => result,
            },
        };
        if let Err(error) = result {
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

/// Applied/Failed, or no status when a privileged update's outcome is
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
