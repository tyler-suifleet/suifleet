mod apply;
mod config;
mod sui_client;
mod walrus_client;

use std::{path::PathBuf, time::Duration};

use apply::ApplyRunner;
use config::Config;
use sui_client::SuiClient;
use tracing::{error, info, warn};
use walrus_client::WalrusClient;

const DEFAULT_CONFIG: &str = "/etc/suifleet/config.toml";

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "suifleet=info".into()),
        )
        .init();

    let config_path = std::env::var("EDGE_CONFIG")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_CONFIG));

    info!("Loading config from {}", config_path.display());
    let cfg = Config::load(&config_path)?;

    let sui = SuiClient::load(cfg.sui.clone(), &cfg.device.keypair_path)?;
    let walrus = WalrusClient::new(cfg.walrus.aggregator_url.clone());
    let runner = ApplyRunner::new(cfg.apply.command.clone());

    let device_object_id = cfg.device.device_object_id.clone();
    let device_cap_id = cfg.device.device_cap_id.clone();
    let package_id = cfg.sui.package_id.clone();
    let group_ids = cfg.device.group_ids.clone();

    let (event_tx, mut event_rx) = tokio::sync::mpsc::channel(32);

    // Spawn the WebSocket subscriber in the background (SuiClient is cheap to clone)
    let sui_sub = sui.clone();
    tokio::spawn(async move {
        sui_sub.subscribe_deployments(&group_ids, event_tx).await;
    });

    // Watchdog keepalive
    #[cfg(feature = "systemd")]
    tokio::spawn(async {
        loop {
            sd_notify::notify(false, &[sd_notify::NotifyState::Watchdog]).ok();
            tokio::time::sleep(Duration::from_secs(30)).await;
        }
    });

    info!("Edge client running. Device: {}", device_object_id);
    notify_ready();

    while let Some(event) = event_rx.recv().await {
        info!(
            "Processing deployment {} (blob {})",
            event.deployment_id, event.walrus_blob_id
        );

        let tmp = std::env::temp_dir().join(format!("{}.artifact", &event.deployment_id[..16]));
        let status = match handle_deployment(&walrus, &runner, &event, &tmp).await {
            Ok(()) => {
                info!("Deployment {} applied successfully", event.deployment_id);
                1u8 // STATUS_APPLIED
            }
            Err(e) => {
                error!("Deployment {} failed: {e}", event.deployment_id);
                2u8 // STATUS_FAILED
            }
        };

        if let Err(e) = sui
            .report_status(
                &package_id,
                "",
                &event.deployment_id,
                &device_cap_id,
                status,
            )
            .await
        {
            warn!("Failed to report status on-chain: {e}");
        }

        let _ = std::fs::remove_file(&tmp);
    }

    Ok(())
}

async fn handle_deployment(
    walrus: &WalrusClient,
    runner: &ApplyRunner,
    event: &sui_client::DeploymentCreatedEvent,
    tmp: &std::path::Path,
) -> anyhow::Result<()> {
    let actual_hash = walrus.download(&event.walrus_blob_id, tmp).await?;

    anyhow::ensure!(
        actual_hash == event.sha256_hash,
        "SHA-256 mismatch: expected {} got {}",
        event.sha256_hash,
        actual_hash
    );

    runner.apply(tmp).await
}

fn notify_ready() {
    #[cfg(feature = "systemd")]
    sd_notify::notify(false, &[sd_notify::NotifyState::Ready]).ok();
}
