mod apply;
mod config;
mod firmware;
mod identity;
mod recipe;
mod protocol;
#[cfg(test)]
mod protocol_tests;
mod state;
mod sui_client;
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
    let check_config = args.len() == 3 && args[1] == "--check-config";
    let helper = args.len() == 3 && args[1] == "--firmware-helper";
    anyhow::ensure!(
        args.len() == 1 || check_config || helper,
        "usage: suifleet [--provision KEY_PATH | --derive-address KEY_PATH | --check-config CONFIG_PATH | --firmware-helper CONFIG]"
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
    let sui = SuiClient::load(cfg.sui.clone(), &cfg.device.keypair_path)?;
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
        let cycle = poll(&cfg, &sui, &walrus, &runner, &mut journal);
        tokio::select! {
            result = cycle => { if let Err(error) = result { warn!(%error, "Sync failed; retrying without advancing cursor"); } }
            _ = &mut shutdown => { info!("Stopping; interrupted apply requires inspection on restart"); break; }
        }
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(cfg.runtime.poll_seconds)) => {}
            _ = &mut shutdown => break,
        }
    }
    Ok(())
}

async fn poll(
    cfg: &Config,
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
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
                ensure_deployment_schema(&record.fields)?;
                let directory = cfg
                    .runtime
                    .state_dir
                    .join("artifacts")
                    .join(hex::encode(identity::hash(event.deployment_id.as_bytes())));
                std::fs::create_dir_all(&directory)?;
                let artifact = directory.join("artifact");
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
                let result =
                    handle_deployment(sui, walrus, runner, &cfg.apply, &event, &artifact).await;
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

async fn handle_deployment(
    sui: &SuiClient,
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    config: &config::ApplyConfig,
    event: &DeploymentCreatedEvent,
    artifact: &Path,
) -> anyhow::Result<()> {
    let digest_field = sui
        .dynamic_field(&event.deployment_id, "deployment_manager::RecipeDigestKey")
        .await?;
    let expected = digest_field.as_ref().and_then(|value| value.as_str())
        .ok_or_else(|| anyhow::anyhow!("deployment has no authenticated recipe digest; recreate using verified creation API"))?;
    let (recipe, actual) = walrus.recipe(&event.recipe_blob_id).await?;
    anyhow::ensure!(actual == expected, "recipe SHA-256 mismatch");
    recipe.validate(event, config.allow_recipe_commands)?;
    let actual_hash = walrus.download(&recipe.artifact_blob_id, artifact).await?;
    anyhow::ensure!(
        actual_hash == event.artifact_sha256,
        "SHA-256 mismatch: expected {} got {}",
        event.artifact_sha256,
        actual_hash
    );
    runner
        .apply(
            artifact,
            recipe.lifecycle.install.as_deref(),
            &firmware::request_id(&event.deployment_id),
        )
        .await
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

fn ensure_deployment_schema(fields: &serde_json::Value) -> anyhow::Result<()> {
    let version = fields["schema_version"].as_str().and_then(|s| s.parse::<u64>().ok())
        .or_else(|| fields["schema_version"].as_u64());
    anyhow::ensure!(version == Some(protocol::DEPLOYMENT_SCHEMA_VERSION), "unsupported DeploymentRecord schema version");
    Ok(())
}
#[cfg(test)]
mod deployment_schema_tests {
    use super::*;
    #[test]
    fn reject_unknown_or_missing_schema_before_apply() {
        for value in [serde_json::json!({}), serde_json::json!({"schema_version":"2"}), serde_json::json!({"schema_version":"invalid"})] {
            assert!(ensure_deployment_schema(&value).is_err());
        }
        assert!(ensure_deployment_schema(&serde_json::json!({"schema_version":"1"})).is_ok());
    }
}
