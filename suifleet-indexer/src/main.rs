//! SuiFleet fleet indexer, versioned API and operations CLI (SF-024).
//!
//!   suifleet-indexer serve --manifest deployment.json --db fleet.db
//!                          [--listen 127.0.0.1:8650] (--tokens FILE | --public-read)
//!                          [--interval-seconds 2] [--reconcile-batch 500] [--reset]
//!   suifleet-indexer index --manifest deployment.json --db fleet.db [--reconcile-batch 500]
//!                          [--reset] [--rebuild-from-objects]
//!   suifleet-indexer ctl ...   (see `suifleet-indexer ctl help`)
//!
//! docs/INDEXER.md covers deployment, auth, storage, retention and recovery.
mod api;
mod chain;
mod ctl;
mod devices;
mod indexer;
mod manifest;
mod store;
mod txbuild;

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use tracing::info;

const USAGE: &str = "usage: suifleet-indexer serve|index|ctl ... (see the header of src/main.rs or docs/INDEXER.md)";

fn options(args: &[String]) -> anyhow::Result<(BTreeMap<String, String>, Vec<String>)> {
    let switches = ["--reset", "--public-read", "--rebuild-from-objects"];
    let mut values = BTreeMap::new();
    let mut flags = Vec::new();
    let mut items = args.iter();
    while let Some(item) = items.next() {
        if switches.contains(&item.as_str()) {
            flags.push(item.clone());
        } else if item.starts_with("--") {
            let value = items
                .next()
                .ok_or_else(|| anyhow::anyhow!("{item} needs a value"))?;
            values.insert(item.clone(), value.clone());
        } else {
            anyhow::bail!("unexpected argument {item}\n{USAGE}");
        }
    }
    Ok((values, flags))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "suifleet_indexer=info".into()),
        )
        .with_writer(std::io::stderr)
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some((command, rest)) = args.split_first() else {
        anyhow::bail!(USAGE);
    };
    if command == "ctl" {
        return ctl::main(rest).await;
    }
    let (values, flags) = options(rest)?;
    let required = |name: &str| {
        values
            .get(name)
            .cloned()
            .ok_or_else(|| anyhow::anyhow!("{name} is required\n{USAGE}"))
    };
    let manifest = manifest::Manifest::load(&PathBuf::from(required("--manifest")?))?;
    let db_path = PathBuf::from(required("--db")?);
    let db = store::open(&db_path)?;
    let mut indexer =
        indexer::Indexer::open(&manifest, db, flags.iter().any(|f| f == "--reset")).await?;
    if let Some(batch) = values.get("--reconcile-batch") {
        indexer.reconcile_batch = batch.parse()?;
        anyhow::ensure!(indexer.reconcile_batch > 0, "--reconcile-batch must be positive");
    }
    match command.as_str() {
        "index" => {
            if flags.iter().any(|f| f == "--rebuild-from-objects") {
                indexer.rebuild_from_objects().await?;
            }
            let changed = indexer.sync_full().await?;
            info!(changed, "Index is at the chain tip");
        }
        "serve" => {
            let public_read = flags.iter().any(|f| f == "--public-read");
            let tokens = match values.get("--tokens") {
                Some(path) => api::Tokens::parse(&std::fs::read_to_string(path)?)?,
                None => {
                    anyhow::ensure!(
                        public_read,
                        "pass --tokens FILE (or --public-read for local, read-only use)"
                    );
                    api::Tokens::parse("")?
                }
            };
            let chain = chain::Chain::new(&manifest.rpc_url)?;
            let types = manifest.verify(&chain).await?;
            let state = api::AppState {
                readers: store::Readers::new(&db_path),
                tokens,
                public_read,
                chain,
                call_package: chain::normalize(&manifest.call_package_id).unwrap_or_default(),
                registry: chain::normalize(&manifest.shared_objects.registry.id).unwrap_or_default(),
                types: Arc::new(types),
            };
            let interval = std::time::Duration::from_secs(
                values
                    .get("--interval-seconds")
                    .map(|s| s.parse())
                    .transpose()?
                    .unwrap_or(2),
            );
            // Catch up (and re-read every device) before serving, so the API
            // never answers from an empty or pre-restart index.
            indexer.sync_full().await?;
            let listen = values
                .get("--listen")
                .cloned()
                .unwrap_or_else(|| "127.0.0.1:8650".into());
            let listener = tokio::net::TcpListener::bind(&listen).await?;
            info!(%listen, public_read, "Serving the fleet API");
            tokio::select! {
                result = indexer::run(indexer, interval) => result?,
                result = axum::serve(listener, api::router(state)) => result?,
                _ = tokio::signal::ctrl_c() => info!("Stopping"),
            }
        }
        _ => anyhow::bail!(USAGE),
    }
    Ok(())
}
