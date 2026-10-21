mod ipc;
mod catalog;
// Linux only (prctl, openat2), like its catalog platforms.
#[cfg(target_os = "linux")]
mod cloudwatch_logs;
mod clientdevices;
mod client_gateway;
mod control;
mod apply;
mod bridge;
mod components;
mod config;
mod configuration;
mod deps;
mod firmware;
mod handoff;
mod identity;
mod iotcore;
mod logs;
mod mqtt;
mod platform;
mod reconcile;
mod seal;
mod seal_client;
mod recipe;
mod redact;
mod protocol;
mod pubsub;
#[cfg(test)]
mod protocol_tests;
#[cfg(test)]
mod compat_tests;
mod release;
mod signer_service;
mod state;
mod sui_client;
mod supervisor;
mod streams;
mod services;
mod shadows;
mod telemetry;
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
    if (args.len() == 4 || args.len() == 5) && args[1] == "--verify-release" {
        let (bundle, trust) = (Path::new(&args[2]), Path::new(&args[3]));
        let verified = match args.get(4) {
            Some(target) => release::verify_target(bundle, trust, target)?,
            None => release::verify(bundle, trust)?,
        };
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
    let cloudwatch_logs = args.len() == 3 && args[1] == "--cloudwatch-logs";
    anyhow::ensure!(
        args.len() == 1 || launched || check_config || helper || signer_service || cloudwatch_logs,
        "usage: suifleet [--launch | --provision KEY_PATH | --derive-address KEY_PATH | --enroll ENROLL_TOML | --check-config CONFIG_PATH | --self-check CONFIG_PATH | --firmware-helper CONFIG | --signer-service SIGNER_TOML | --cloudwatch-logs CONFIG_JSON | --record-main-process PATH | --key-holder CONFIG_PATH | --verify-release BUNDLE_DIR TRUST_FILE [TARGET]]"
    );
    {
        use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};
        // The second layer writes the daemon's own log file once [logs] is on.
        tracing_subscriber::registry()
            .with(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "suifleet=info".into()))
            .with(tracing_subscriber::fmt::layer())
            .with(tracing_subscriber::fmt::layer().with_ansi(false).with_writer(logs::DaemonLog))
            .init();
    }
    if signer_service {
        // Runs as its own user, holding the device key (SF-053).
        return signer_service::serve(Path::new(&args[2]));
    }
    if cloudwatch_logs {
        // Runs as its own user with scoped AWS credentials (SF-060).
        #[cfg(target_os = "linux")]
        return cloudwatch_logs::run(Path::new(&args[2])).await;
        #[cfg(not(target_os = "linux"))]
        anyhow::bail!("the CloudWatch Logs adapter runs on Linux only");
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
    let mut cfg = Config::load(&path)?;
    if let Some(iot) = &cfg.aws_iot {
        // Unusable AWS IoT credentials stop startup; rotation is checked while running.
        iotcore::Credentials::load(iot)?;
    }
    logs::set_group_read(cfg.runtime.log_group_read);
    if !check_config {
        // Also when no log is written any more: turning the option off
        // makes the existing logs private again.
        logs::reconcile_modes(&cfg.runtime.state_dir)?;
    }
    if (cfg.logs.is_some() || cfg.runtime.log_group_read) && !check_config {
        // From here on the daemon's own log is also written for shipping
        // or for a separate log reader.
        logs::enable_daemon_log(&cfg.runtime.state_dir)?;
    }
    let started_ms = telemetry::now_ms();
    if !check_config && cfg.device.signer.as_ref().is_some_and(|signer| signer.kind == "service") {
        // Component processes run as this user: with the key in the signing
        // service, keep them from ptracing the daemon (and so signing as it)
        // or taking its descriptors. execve restores the default for each
        // component.
        // (Linux only: configuration loading refuses this signer elsewhere.)
        // SAFETY: plain prctl on this process.
        #[cfg(target_os = "linux")]
        anyhow::ensure!(unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) } == 0, "cannot make the daemon non-dumpable");
    }
    #[cfg(target_os = "macos")]
    if cfg.device.signer.is_none() {
        identity::ensure_private(&cfg.device.keypair_path)?;
    }
    // One signer for transactions and telemetry (a PKCS#11 module is
    // initialized once per process).
    let signer = identity::signer(&cfg.device)?;
    let sui = SuiClient::load(cfg.sui.clone(), signer.clone())?;
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
    let walrus = WalrusClient::new(&cfg.walrus).with_seal(confidential_delivery(&cfg, &sui, signer.clone())?);
    // The artifact cache holds at most the in-flight artifact; anything left
    // by an earlier run (e.g. a crash mid-download) is unused.
    let freed = walrus_client::collect_garbage(&cfg.runtime.state_dir.join("artifacts"))?;
    if freed > 0 {
        info!(
            bytes = freed,
            "Removed leftover artifacts from a previous run"
        );
    }
    // Before any component runs: their log lines are covered from here on.
    logs::initialize(&cfg.runtime.state_dir)?;
    if cfg.runtime.log_group_read && cfg.logs.is_none() {
        // The shipper would record these each interval; a separate reader
        // only has what the daemon records.
        logs::remember_materialized(&cfg.runtime.state_dir)?;
    }
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
    // Resolve completed application recovery before service tasks or stream
    // loading can observe the superseded generation after a crash.
    let recovered = supervisor.recoveries_for_start();
    for name in recovered.keys() { configs.restore(name, supervisor.configuration(name)?.as_ref())?; }
    services::startup_recovery(&cfg, &mut journal, &recovered)?;
    let mut ipc_server = cfg.ipc.as_ref().map(ipc::bind).transpose()?;
    let mut ipc_messages = ipc::Messages::default();
    let service_runtime = std::sync::Arc::new(std::sync::Mutex::new(services::Runtime::open(
        &cfg, &journal.state.services, signer.clone(), streams::Binding {
            chain_identifier: sui.manifest().chain_identifier.clone(),
            registry: sui.manifest().shared_objects.registry.id.clone(),
            device_id: cfg.device.device_object_id.clone(),
        })?));
    cfg.services = Some(service_runtime.clone());
    let streams = service_runtime.lock().unwrap().streams.clone();
    let durable = service_runtime.lock().unwrap().broker.clone();
    // Device shadows (SF-033); changes are announced on durable pub/sub.
    let shadow_store = match &cfg.shadows {
        Some(settings) => Some(std::sync::Arc::new(std::sync::Mutex::new(shadows::Store::open(
            &cfg.runtime.state_dir.join("shadows"), settings.max_shadows, &settings.thing_name, durable.clone())?))),
        None => None,
    };
    service_runtime.lock().unwrap().shadow_store = shadow_store.clone();
    // LAN client devices (SF-035): the CA and registry, then the gateway's
    // listeners (bound here, so a bad address stops startup).
    let client_devices = match &cfg.client_devices {
        Some(settings) => {
            let authority = clientdevices::Authority::open(
                &cfg.runtime.state_dir.join("clientdevices"), settings.clone(),
                clientdevices::Gateway { chain: sui.manifest().chain_identifier.clone(),
                    registry: sui.manifest().shared_objects.registry.id.clone(), device_object_id: cfg.device.device_object_id.clone(), address: sui.address() },
                signer.clone(), telemetry::now_ms())?;
            if let Some(bridge) = service_runtime.lock().unwrap().effective.pubsub.as_ref().and_then(|p| p.bridge.as_ref()) {
                authority.set_bridge_client_id(Some(&bridge.client_id));
            }
            Some(std::sync::Arc::new(authority))
        }
        None => None,
    };
    let gateway_task = match &client_devices {
        Some(authority) => Some(client_gateway::start(authority.clone()).await?),
        None => None,
    };
    service_runtime.lock().unwrap().client_devices = client_devices.clone();
    let runner = ApplyRunner::new(cfg.apply.clone());
    let store = components::Store::open(&cfg.runtime.state_dir, &cfg.components);
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
    // Telemetry runs beside the poll loop, so a long apply or bootstrap does
    // not silence the heartbeat; it reads only the state files on disk.
    let sync = telemetry::SharedSync::default();
    let reporter = match &cfg.telemetry {
        Some(settings) => Some(tokio::spawn(
            telemetry::Task {
                binding: telemetry::Binding {
                    chain_identifier: sui.manifest().chain_identifier.clone(),
                    registry: sui.manifest().shared_objects.registry.id.clone(),
                    device_id: cfg.device.device_object_id.clone(),
                    interval_seconds: settings.interval_seconds,
                    poll_seconds: cfg.runtime.poll_seconds,
                    started_ms,
                    boot_id: telemetry::boot_id(),
                },
                url: telemetry::ingress_url(&settings.url)?,
                state_dir: cfg.runtime.state_dir.clone(),
                buffer_max: settings.buffer_max,
                signer: signer.clone(),
                sync: sync.clone(),
            }
            .run(),
        )),
        None => {
            info!("No [telemetry] configured; device health is not reported");
            None
        }
    };
    // Redacted remote logs beside it (validated to have [telemetry]).
    let shipper = match (&cfg.logs, &cfg.telemetry) {
        (Some(settings), Some(telemetry)) => Some(tokio::spawn(
            logs::Task {
                binding: logs::Binding {
                    chain_identifier: sui.manifest().chain_identifier.clone(),
                    registry: sui.manifest().shared_objects.registry.id.clone(),
                    device_id: cfg.device.device_object_id.clone(),
                },
                url: logs::ingress_url(&telemetry.url)?,
                state_dir: cfg.runtime.state_dir.clone(),
                overlay_dir: cfg.components.configuration_overlay_dir.clone(),
                interval_seconds: settings.interval_seconds,
                max_lines: settings.max_lines_per_interval,
                buffer_max: settings.buffer_max,
                signer: signer.clone(),
            }
            .run(),
        )),
        _ => None,
    };
    // AWS IoT Device Shadow sync (SF-061) runs over the bridge's [aws_iot]
    // connection; the service runtime owns that connection side.
    let aws_shadow_sync = cfg.shadows.as_ref().and_then(|s| s.aws_iot.clone())
        .zip(service_runtime.lock().unwrap().aws_shadow_client.take());
    let shadow_sync = match (&cfg.shadows, &shadow_store, aws_shadow_sync) {
        (Some(_), Some(store), Some((sync, client))) => {
            let store = store.clone();
            Some(tokio::spawn(async move {
                let changed = client.changed.clone();
                shadows::run_sync(store, &sync.shadows, sync.interval_seconds, sync.max_backoff_seconds, sync.owners(),
                                  &iotcore::AwsShadows(client), Some(changed)).await
            }))
        }
        (Some(settings), Some(store), None) => settings.sync.clone().map(|config| tokio::spawn(shadows::Syncer {
            store: store.clone(),
            config,
            binding: streams::Binding {
                chain_identifier: sui.manifest().chain_identifier.clone(),
                registry: sui.manifest().shared_objects.registry.id.clone(),
                device_id: cfg.device.device_object_id.clone(),
            },
            signer: signer.clone(),
        }.run())),
        _ => None,
    };
    // A new daemon is confirmed only after this complete local startup
    // (configuration, identity, chain validation, journal and supervisor).
    if let Some(update) = confirm_after_startup.take() {
        handoff::confirm(&handoff_dir, &update)?;
        info!(deployment = update.deployment, version = update.version, "Daemon update confirmed");
        settle_handoff(&mut journal, &handoff_dir, &update, Ok(()))?;
    }
    let mut restart_for_handoff = false;
    let mut restart_for_services = false;
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
                    Err(error) if error.downcast_ref::<services::RecoveryRequired>().is_some() => {
                        warn!(%error, "Restarting to recover the service generation");
                        restart_for_services = true;
                        break;
                    }
                    result => {
                        sync.lock().unwrap().record(&result);
                        if let Err(error) = result { warn!(%error, "Sync failed; retrying without advancing cursor"); }
                    }
                },
                _ = &mut shutdown => { info!("Stopping; interrupted apply requires inspection on restart"); break; }
            }
            let delay = tokio::time::sleep(Duration::from_secs(cfg.runtime.poll_seconds));
            tokio::pin!(delay);
            let mut stopped = false;
            loop {
                tokio::select! {
                    _ = &mut delay => break,
                    _ = &mut shutdown => { stopped = true; break; }
                    Some(pending) = async {
                        match ipc_server.as_mut() {
                            Some(server) => server.requests.recv().await,
                            None => std::future::pending().await,
                        }
                    } => {
                        if pending.reply.is_closed() { continue; }
                        let result = tokio::select! {
                            result = local_request(&cfg, &sui, &walrus, &runner, store.as_ref(), &configs,
                                &mut supervisor, &mut journal, &mut ipc_messages, durable.as_deref(), streams.as_deref(), shadow_store.as_deref(),
                                client_devices.as_deref(), pending.uid, &pending.request) => result,
                            _ = &mut shutdown => { stopped = true; break; }
                        };
                        if result.as_ref().err().is_some_and(|e| e.downcast_ref::<services::RecoveryRequired>().is_some()) {
                            let _ = pending.reply.send(ipc::response(result));
                            restart_for_services = true;
                            stopped = true;
                            break;
                        }
                        if let Err(error) = &result { warn!(operation = pending.request.operation, %error, "Local IPC request failed"); }
                        let _ = pending.reply.send(ipc::response(result));
                    }
                }
            }
            if stopped { break; }
        }
    }
    // Spooled snapshots stay for the next start.
    if let Some(shipper) = shipper {
        shipper.abort();
    }
    if let Some(reporter) = reporter {
        reporter.abort();
    }
    // Every shadow change is already on disk; sync resumes from its base.
    if let Some(task) = shadow_sync {
        task.abort();
    }
    if let Some(store) = &streams {
        store.lock().unwrap().close();
    }
    if let Some(task) = gateway_task {
        task.abort();
    }
    // Components stop with the daemon (shutdown command, then the whole group).
    supervisor.stop_all().await;
    // process::exit below does not run destructors: close IPC before handoff.
    drop(ipc_server);
    if restart_for_services { anyhow::bail!("restart required to recover service configuration"); }
    if restart_for_handoff {
        std::process::exit(handoff::RESTART_EXIT);
    }
    Ok(())
}

/// Confidential delivery (SF-022): Seal sessions certified by this device's
/// own identity, held by the signing service when the daemon has no key.
fn confidential_delivery(
    cfg: &Config,
    sui: &SuiClient,
    signer: std::sync::Arc<dyn identity::TxSigner>,
) -> anyhow::Result<Option<std::sync::Arc<seal_client::Seal>>> {
    let Some(config) = &cfg.seal else { return Ok(None) };
    let authority: std::sync::Arc<dyn seal_client::Authority> = match &cfg.device.signer {
        Some(service) if service.kind == "service" => std::sync::Arc::new(signer_service::ServiceSessions::new(&service.socket)),
        _ => std::sync::Arc::new(seal_client::Sessions::new(
            signer,
            seal_client::SessionPolicy {
                package: std::str::FromStr::from_str(sui.origin())?,
                ttl_min: config.session_ttl_minutes,
                clock_skew_ms: config.clock_skew_seconds * 1000,
            },
        )?),
    };
    let seal = seal_client::Seal::new(config.clone(), sui.clone(), authority, &cfg.device.device_object_id)?;
    info!(mode = ?config.mode, key_servers = config.key_servers.len(), "Confidential delivery enabled");
    Ok(Some(std::sync::Arc::new(seal)))
}

/// Executed by the poll-loop owner: IPC never races a deployment or a journal save.
async fn local_request(
    cfg: &Config, sui: &SuiClient, walrus: &WalrusClient, runner: &ApplyRunner,
    store: Option<&components::Store>, configs: &configuration::Store,
    supervisor: &mut supervisor::Supervisor, journal: &mut Journal,
    messages: &mut ipc::Messages, durable: Option<&std::sync::Mutex<pubsub::Broker>>,
    streams: Option<&std::sync::Mutex<streams::Store>>, shadow_store: Option<&std::sync::Mutex<shadows::Store>>,
    client_devices: Option<&clientdevices::Authority>,
    uid: u32, request: &ipc::Request,
) -> anyhow::Result<serde_json::Value> {
    use serde_json::json;
    let runtime = cfg.services.as_ref().ok_or_else(|| anyhow::anyhow!("service runtime missing"))?;
    let (mut effective, live_streams, live_broker, summary) = {
        let runtime = runtime.lock().unwrap();
        (runtime.effective.clone(), runtime.streams.clone(), runtime.broker.clone(), runtime.summary())
    };
    effective.services = cfg.services.clone();
    let cfg = &effective;
    let streams = live_streams.as_deref().or(streams);
    let durable = live_broker.as_deref().or(durable);
    let streams = cfg.streams.as_ref().and(streams);
    let durable = cfg.pubsub.as_ref().and(durable);
    let policy = cfg.ipc.as_ref().ok_or_else(|| anyhow::anyhow!("IPC disabled"))?;
    let principal = policy.authorize(uid, request)?;
    let name = &request.component;
    match request.operation.as_str() {
        "list" => Ok(json!({"installed":journal.state.components,"supervised":supervisor.status()})),
        "status" => Ok(json!({"installed":journal.state.components.get(name),
            "runtime":supervisor.status().get(name),"reported_health":messages.health(name)})),
        "start" | "stop" | "restart" => {
            supervisor.local_control(name, &request.operation).await?;
            Ok(json!({"runtime":supervisor.status().get(name)}))
        }
        "logs" => Ok(json!({"tail":supervisor.local_logs(name)?})),
        "config" => Ok(json!({"configuration":configs.committed(name)?,"services":summary})),
        "messaging" => {
            let subscriptions = messages.handle(policy, request, &principal, durable)?;
            let bridge = std::fs::read(cfg.runtime.state_dir.join("pubsub/bridge.json")).ok()
                .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok());
            Ok(json!({"subscriptions":subscriptions["subscriptions"],"bridge":bridge}))
        }
        "health" | "publish" | "receive" | "subscribe" | "unsubscribe" | "ack" =>
            messages.handle(policy, request, &principal, durable),
        // Administrators only (component ACLs cannot list it).
        "client_devices" => client_devices.ok_or_else(|| anyhow::anyhow!("client devices are not enabled ([client_devices])"))?
            .handle(&request.payload, telemetry::now_ms()),
        op if op.starts_with("stream_") => {
            let store = streams.ok_or_else(|| anyhow::anyhow!("streams are not enabled ([streams])"))?;
            let declared: Vec<String> = cfg.streams.iter().flat_map(|s| s.definitions.iter().map(|d| d.name.clone())).collect();
            let acl = policy.components.get(&principal);
            let visible = |name: &str| acl.map_or(true, |a| a.streams.iter().any(|p| streams::glob(p, name)));
            streams::handle(store, &declared, op, &request.topic, &request.payload, &visible)
        }
        "shadow_get" | "shadow_update" | "shadow_delete" | "shadow_list" | "shadows" => {
            let store = shadow_store.ok_or_else(|| anyhow::anyhow!("shadows are not enabled ([shadows])"))?;
            // Administrators (no component ACL) may use every shadow.
            let policies = policy.components.get(&principal).map(|acl| acl.shadow_policies.as_slice());
            shadows::handle(store, policies, &request.operation, &request.topic, &request.payload)
        }
        "install" => {
            let id = request.payload.as_str().ok_or_else(|| anyhow::anyhow!("deployment ID required"))?;
            anyhow::ensure!(id.starts_with("0x") && id.len() <= 66 && id.len() > 2
                && id[2..].bytes().all(|b| b.is_ascii_hexdigit()), "invalid deployment ID");
            let canonical_id = format!("0x{:0>64}", id[2..].to_ascii_lowercase());
            let id = canonical_id.as_str();
            sui.validate_deployment().await?;
            sui.validate_cap(&cfg.device.device_cap_id, &cfg.device.device_object_id).await?;
            let (event, record) = sui.deployment(id).await?.ok_or_else(|| anyhow::anyhow!("missing deployment"))?;
            anyhow::ensure!(sui.authorized_target(&event, &cfg.device.device_object_id).await?, "unauthorized target");
            anyhow::ensure!(!sui.is_managed(id).await?, "desired-state deployments belong to reconciliation");
            anyhow::ensure!(!cfg.daemon_update.enabled || event.component_name != cfg.daemon_update.component,
                "daemon updates belong to reconciliation");
            // No replay/force operation: ambiguous destructive work is never rerun.
            if let Some((_, outcome)) = journal.state.deployments.iter().find(|(key, _)| sui_client::same_id(key, id)) {
                return Ok(json!({"outcome":outcome}));
            }
            let status = execute(cfg, sui, walrus, runner, store, configs, supervisor, journal, &event, &record).await?;
            settle_configuration(configs, journal, supervisor)?;
            Ok(json!({"status":status,"outcome":journal.state.deployments.get(id),"deferred":journal.state.deferred.get(id)}))
        }
        _ => anyhow::bail!("unknown operation"),
    }
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
    let old_services = journal.state.services.clone();
    if services::reconcile(&mut journal.state, supervisor.recovered()) {
        let next = std::mem::replace(&mut journal.state.services, old_services);
        services::commit(cfg, journal, next, None).await.map_err(|error| services::RecoveryRequired(format!("service reconciliation failed: {error:#}")))?;
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
    if !admit(sui, journal, &event.deployment_id, &cfg.device.device_object_id).await? {
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
    if result.as_ref().err().is_some_and(|e| e.downcast_ref::<services::RecoveryRequired>().is_some()) { return result.map(|_| None); }
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
        let authentic = components::recipe(walrus, store, &event.recipe_blob_id, &resolved.recipe_sha256).await?;
        authentic.check("validation", |recipe| {
            let dependencies = recipe.validate(event, false)?;
            let lifecycle = &recipe.lifecycle;
            anyhow::ensure!(
                recipe.services.is_none() && dependencies.is_empty() && lifecycle.install.is_none() && lifecycle.run.is_none() && lifecycle.shutdown.is_none(),
                "a daemon update recipe carries only the daemon binary (no lifecycle commands or dependencies)"
            );
            Ok(())
        })?;
        let recipe = &authentic.recipe;
        let artifact = directory.join("artifact");
        components::artifact(walrus, store, &recipe.artifact_blob_id, &event.artifact_sha256, &artifact).await?;
        // As in handle_deployment: re-read after downloads, before the
        // candidate runs or the daemon restarts. Read failure fails closed.
        match admit(sui, journal, &event.deployment_id, &cfg.device.device_object_id).await {
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
async fn admit(sui: &SuiClient, journal: &mut Journal, id: &str, device: &str) -> anyhow::Result<bool> {
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
    let reason = match control.reason(now, notified)? {
        Some(reason) => Some(reason),
        None => sui.rollout_admission(id, device).await?,
    };
    let entry = control::Deferred {
        control, notified_ms: notified,
        reason: reason.unwrap_or("ready for admission").to_owned(),
        terminal: matches!(reason, Some("cancelled" | "expired" | "rollout aborted" | "rollout complete")),
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
    /// Its recipe was Seal-encrypted: configuration errors are withheld.
    confidential: bool,
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
    let authentic = components::recipe(walrus, store, &event.recipe_blob_id, &resolved.recipe_sha256).await?;
    let (dependencies, root_supervised) = authentic.check("validation", |recipe| {
        let dependencies = recipe.validate(event, config.allow_recipe_commands)?;
        anyhow::ensure!(recipe.services.is_none() || config.mode == config::ApplyMode::Direct || recipe.lifecycle.install.is_some() || recipe.lifecycle.run.is_some(),
            "asynchronous firmware-only updates cannot change component services; use a component deployment");
        Ok((dependencies, supervisor::Lifecycle::of(&recipe.lifecycle)?))
    })?;
    let recipe = &authentic.recipe;
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
    let plan = components::plan(&mut loader, &root, &installed).await;
    let plan = components::withhold(authentic.confidential || loader.confidential, "dependency resolution", plan)?;
    if !plan.omitted.is_empty() {
        info!(deployment = event.deployment_id, omitted = ?plan.omitted, "Unavailable soft dependencies omitted");
    }
    let plan_components = plan.order.iter().map(|p| p.name.clone()).collect();
    let runtime = cfg.services.as_ref().ok_or_else(|| anyhow::anyhow!("service runtime missing"))?;
    // Service names, topics and streams come from the recipe: withheld when confidential.
    let service_next = components::withhold(authentic.confidential || loader.confidential, "service validation", (|| {
        let next = services::candidate(&journal.state.services, &event.component_name, &event.deployment_id,
            recipe.services.as_ref(), &plan_components)?;
        runtime.lock().unwrap().prepare(&next)?;
        Ok(next)
    })())?;
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
        let (component, blob, install, supervised, configuration, validate, confidential) = if root {
            (
                component(
                    resolved.catalog.as_ref().map(|c| c.version_id.clone()),
                    &resolved.recipe_sha256,
                    &event.artifact_sha256,
                ),
                recipe.artifact_blob_id.clone(),
                recipe.lifecycle.install.clone(),
                root_supervised.clone(),
                recipe.configuration.clone(),
                recipe.lifecycle.validate.clone(),
                authentic.confidential,
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
                loaded.confidential,
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
            confidential,
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
    match admit(sui, journal, &event.deployment_id, &cfg.device.device_object_id).await {
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
    services::commit(cfg, journal, service_next, Some(&event.deployment_id)).await?;
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
            let overlay = configs.overlay(&install.name)?;
            let configuration = &install.configuration;
            let computed = (|| {
                let record = configuration::Record {
                    deployment: deployment.to_owned(),
                    version: install.component.version.clone(),
                    defaults: configuration.defaults.clone().unwrap_or_default(),
                    effective: configuration::next(previous.as_ref().map(|r| &r.effective), configuration)?,
                    confidential: configuration.confidential.clone().unwrap_or_default(),
                };
                let materialized = configuration::materialize(&record, &overlay)?;
                Ok((record, materialized))
            })();
            let (record, materialized) = components::withhold(install.confidential, "configuration merge", computed)?;
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
    let overlay = configs.overlay(&install.name)?;
    let file = directory.join(format!("configuration-{index}.json"));
    let materialized = components::withhold(install.confidential, "configuration merge", configuration::materialize(&install.record, &overlay))?;
    configuration::write_private(&file, &materialized)?;
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
