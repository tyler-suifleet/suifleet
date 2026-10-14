mod apply;
mod config;
mod firmware;
mod identity;
mod recipe;
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
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "suifleet=info".into()),
        )
        .init();
    if args.len() == 3 && args[1] == "--firmware-helper" {
        // Root-owned, path-activated: the only privileged operation.
        let helper = config::FirmwareHelperConfig::load(Path::new(&args[2]))?;
        firmware::run_helper(&helper).await?;
        return Ok(());
    }
    anyhow::ensure!(
        args.len() == 1,
        "usage: suifleet [--provision KEY_PATH | --derive-address KEY_PATH | --firmware-helper CONFIG]"
    );
    let path = std::env::var("EDGE_CONFIG")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/etc/suifleet/config.toml"));
    let cfg = Config::load(&path)?;
    let sui = SuiClient::load(cfg.sui.clone(), &cfg.device.keypair_path)?;
    let binding = format!(
        "{}/{}/{}/{}",
        cfg.sui.rpc_url,
        sui.origin(),
        cfg.device.device_object_id,
        sui.address()
    );
    let mut journal = Journal::open(&cfg.runtime.state_dir, binding)?;
    let walrus = WalrusClient::new(cfg.walrus.aggregator_url.clone());
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
    sui.validate_cap(&cfg.device.device_cap_id, &cfg.device.device_object_id)
        .await?;
    if !journal.state.initialized {
        if !cfg.runtime.replay_history {
            journal.state.cursor = sui.latest_cursor().await?;
        }
        journal.state.initialized = true;
        journal.save()?;
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
        let events = page["data"]
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("invalid event page"))?;
        for raw in events {
            let cursor = raw
                .get("id")
                .filter(|id| id.is_object())
                .ok_or_else(|| anyhow::anyhow!("event missing cursor"))?
                .clone();
            let event: DeploymentCreatedEvent = serde_json::from_value(raw["parsedJson"].clone())?;
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
                    record["content"]["type"].as_str()
                        == Some(&format!(
                            "{}::deployment_manager::DeploymentRecord",
                            sui.origin()
                        )),
                    "wrong deployment type"
                );
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
                        status: None,
                        reported: false,
                        error: None,
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
                outcome.status = Some(if result.is_ok() { 1 } else { 2 });
                outcome.error = result.err().map(|error| format!("{error:#}"));
                info!(deployment = event.deployment_id, status = outcome.status, error = ?outcome.error, "Deployment finished");
                journal.save()?;
                let _ = std::fs::remove_file(&artifact);
            }
            journal.state.cursor = Some(cursor);
            journal.save()?;
        }
        if !page["hasNextPage"].as_bool().unwrap_or(false) {
            break;
        }
        anyhow::ensure!(!events.is_empty(), "empty event page claims more data");
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
        .rpc(
            "suix_getDynamicFieldObject",
            serde_json::json!([event.deployment_id,
        {"type":format!("{}::deployment_manager::RecipeDigestKey", sui.origin()),"value":{}}]),
        )
        .await?;
    let expected = digest_field["data"]["content"]["fields"]["value"].as_str()
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
        .apply(artifact, recipe.lifecycle.install.as_deref())
        .await
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
