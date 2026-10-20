//! Actionable fleet alerts (SF-020), derived from verified telemetry and the
//! indexed deployment receipts. Each alert has a stable `key`, so a webhook
//! sends it once and later reports it resolved.
//!
//! | kind | severity | when |
//! |---|---|---|
//! | `component-broken` | critical | a supervised component exhausted its restart budget |
//! | `deployment-failed` | critical | the device's on-chain receipt for a deployment is Failed |
//! | `device-offline` | warning | the device stopped reporting |
//! | `device-degraded` | warning | the device reports problems, or its report is delayed or inconsistent |
//! | `deployment-pending` | warning | an online device has not reported a deployment older than the threshold |
//!
//! An online device's newest report also raises the service-health alerts of
//! its services section (SF-085, `crate::services::conditions`).
use crate::store::{self, Device, Filter};
use crate::telemetry::{self, Status};
use rusqlite::{params, Connection};
use serde::Serialize;

/// Default age after which an unreported deployment on an online device alerts.
pub const DEFAULT_STUCK_AFTER_MS: u64 = 60 * 60 * 1000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Alert {
    pub key: String,
    pub kind: &'static str,
    pub severity: &'static str,
    pub device_id: String,
    pub device_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deployment_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub component: Option<String>,
    pub message: String,
    /// What an operator should do.
    pub action: String,
    /// When the condition began, as far as the indexer knows (ms).
    pub since_ms: Option<u64>,
}

fn bounded(text: &str) -> String {
    let mut end = text.len().min(300);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}

/// Alerts for one device.
pub fn for_device(index: &Connection, telemetry: &Connection, device: &Device, now: u64, stuck_after_ms: u64) -> anyhow::Result<Vec<Alert>> {
    let mut alerts = Vec::new();
    if device.deleted || device.revoked || !device.activated {
        return Ok(alerts);
    }
    let health = telemetry::device_health(telemetry, device, now)?;
    let alert = |kind: &'static str, severity: &'static str, subject: &str, message: String, action: &str, since: Option<u64>| Alert {
        key: format!("{kind}:{}:{subject}", device.id),
        kind,
        severity,
        device_id: device.id.clone(),
        device_name: device.name.clone(),
        deployment_id: None,
        component: None,
        message: bounded(&message),
        action: bounded(action),
        since_ms: since,
    };
    let online = matches!(health.assessment.status, Status::Healthy | Status::Degraded);
    match health.assessment.status {
        Status::Offline => alerts.push(alert(
            "device-offline", "warning", "",
            format!("{} has not reported since {} ms", device.name, health.last_seen_ms.unwrap_or(0)),
            "Check the device's power and network; its logs and last report are in the diagnostic bundle.",
            health.last_seen_ms,
        )),
        Status::Degraded => {
            for component in &health.components {
                if component.supervised && component.phase.as_deref() == Some("broken") {
                    let mut a = alert(
                        "component-broken", "critical", &component.name,
                        format!("{} {} exhausted its restart budget: {}", component.name, component.version,
                                component.last_exit.as_deref().unwrap_or("no exit recorded")),
                        "Inspect the component's logs; deploy a fixed version or roll back. It restarts on the next deployment or daemon start.",
                        health.observed_ms,
                    );
                    a.component = Some(component.name.clone());
                    a.deployment_id = Some(component.deployment_id.clone());
                    alerts.push(a);
                }
            }
            alerts.push(alert(
                "device-degraded", "warning", "",
                format!("{}: {}", health.assessment.codes.join(", "), health.assessment.reasons.join("; ")),
                "Review the reasons and the device's recent logs.",
                health.observed_ms,
            ));
        }
        _ => {}
    }
    if online {
        for c in health.report.as_ref().map(|r| crate::services::conditions(&r.snapshot)).unwrap_or_default() {
            alerts.push(alert(c.kind, c.severity, &c.subject, c.message, &c.action, c.since_ms));
        }
    }
    let filter = Filter { device: Some(device.id.clone()), ..Filter::default() };
    let mut cursor = None;
    loop {
        let page = store::list_deployments(index, &filter, cursor.as_deref(), Some(store::MAX_LIMIT))?;
        for deployment in &page.items {
            let receipt = store::read_receipts(index, &deployment.id)?.into_iter().find(|(id, _)| id == &device.id).map(|(_, s)| s);
            let subject = deployment.id.as_str();
            match receipt {
                Some(2) => {
                    let mut a = alert(
                        "deployment-failed", "critical", subject,
                        format!("{} {} failed on {}", deployment.component_name, deployment.version, device.name),
                        "Search the device's logs for the deployment; fix the recipe or artifact and redeploy.",
                        Some(deployment.created_at),
                    );
                    a.deployment_id = Some(deployment.id.clone());
                    a.component = Some(deployment.component_name.clone());
                    alerts.push(a);
                }
                None if online && now.saturating_sub(deployment.created_at) > stuck_after_ms => {
                    let mut a = alert(
                        "deployment-pending", "warning", subject,
                        format!("{} {} has no receipt from online device {} after {} min",
                                deployment.component_name, deployment.version, device.name,
                                now.saturating_sub(deployment.created_at) / 60_000),
                        "Check whether the deployment is paused or scheduled (deployment control), then the device's logs for apply or report errors.",
                        Some(deployment.created_at),
                    );
                    a.deployment_id = Some(deployment.id.clone());
                    a.component = Some(deployment.component_name.clone());
                    alerts.push(a);
                }
                _ => {}
            }
        }
        match page.next {
            Some(next) => cursor = Some(next),
            None => break,
        }
    }
    Ok(alerts)
}

/// Alerts for every device the filter selects, most severe first.
pub fn fleet(index: &Connection, telemetry: &Connection, filter: &Filter, now: u64, stuck_after_ms: u64) -> anyhow::Result<Vec<Alert>> {
    let mut alerts = Vec::new();
    let mut cursor = None;
    loop {
        let page = store::list_devices(index, filter, cursor.as_deref(), Some(store::MAX_LIMIT))?;
        for device in &page.items {
            alerts.extend(for_device(index, telemetry, device, now, stuck_after_ms)?);
        }
        match page.next {
            Some(next) => cursor = Some(next),
            None => break,
        }
    }
    alerts.sort_by(|a, b| (a.severity != "critical", &a.key).cmp(&(b.severity != "critical", &b.key)));
    Ok(alerts)
}

/// New and resolved alerts since the last notification, recorded as sent.
/// Call [`commit`] once the webhook accepted them.
pub fn changes(db: &Connection, current: &[Alert]) -> anyhow::Result<(Vec<Alert>, Vec<String>)> {
    let mut statement = db.prepare_cached("SELECT key FROM alert_state WHERE notified_ms IS NOT NULL")?;
    let notified: std::collections::BTreeSet<String> = statement.query_map([], |row| row.get(0))?.collect::<Result<_, _>>()?;
    let keys: std::collections::BTreeSet<&str> = current.iter().map(|a| a.key.as_str()).collect();
    let new = current.iter().filter(|a| !notified.contains(&a.key)).cloned().collect();
    let resolved = notified.iter().filter(|k| !keys.contains(k.as_str())).cloned().collect();
    Ok((new, resolved))
}

pub fn commit(db: &Connection, new: &[Alert], resolved: &[String], now: u64) -> anyhow::Result<()> {
    let transaction = db.unchecked_transaction()?;
    for alert in new {
        transaction.execute(
            "INSERT INTO alert_state (key, first_ms, notified_ms, body) VALUES (?1, ?2, ?2, ?3)
             ON CONFLICT(key) DO UPDATE SET notified_ms = excluded.notified_ms, body = excluded.body",
            params![alert.key, now as i64, serde_json::to_string(alert)?],
        )?;
    }
    for key in resolved {
        transaction.execute("DELETE FROM alert_state WHERE key = ?1", [key])?;
    }
    transaction.commit()?;
    Ok(())
}

/// `--alert-webhook`: post new and resolved alerts every `interval`.
pub async fn notify_loop(
    readers: store::Readers,
    service: std::sync::Arc<telemetry::Service>,
    url: reqwest::Url,
    interval: std::time::Duration,
    stuck_after_ms: u64,
) {
    let http = match reqwest::Client::builder().timeout(std::time::Duration::from_secs(15)).build() {
        Ok(http) => http,
        Err(error) => return tracing::warn!(%error, "alert webhook disabled"),
    };
    loop {
        if let Err(error) = notify_once(&readers, &service, &http, &url, stuck_after_ms).await {
            tracing::warn!(%error, "alert notification failed; will retry");
        }
        tokio::time::sleep(interval).await;
    }
}

pub async fn notify_once(
    readers: &store::Readers,
    service: &telemetry::Service,
    http: &reqwest::Client,
    url: &reqwest::Url,
    stuck_after_ms: u64,
) -> anyhow::Result<usize> {
    let now = telemetry::now_ms();
    let (new, resolved) = {
        let index = readers.open()?;
        let telemetry = service.reader()?;
        let current = fleet(&index, &telemetry, &Filter::default(), now, stuck_after_ms)?;
        changes(&telemetry, &current)?
    };
    if new.is_empty() && resolved.is_empty() {
        return Ok(0);
    }
    let response = http.post(url.clone()).json(&serde_json::json!({ "alerts": new, "resolved": resolved, "sent_ms": now })).send().await?;
    anyhow::ensure!(response.status().is_success(), "alert webhook answered {}", response.status());
    commit(&service.writer.lock().unwrap(), &new, &resolved, now)?;
    Ok(new.len() + resolved.len())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Deployment;
    use crate::telemetry::tests::{address, binding, device, sign, snapshot, KEY};

    struct Fleet {
        _dir: tempfile::TempDir,
        index: Connection,
        telemetry: Connection,
    }

    fn setup() -> Fleet {
        let dir = tempfile::tempdir().unwrap();
        let mut index = store::open(&dir.path().join("fleet.db")).unwrap();
        let tx = index.transaction().unwrap();
        store::put_device(&tx, &device(&address(&KEY)), None).unwrap();
        tx.commit().unwrap();
        let telemetry = telemetry::open(&dir.path().join("telemetry.db"), &binding(), false).unwrap();
        Fleet { _dir: dir, index, telemetry }
    }

    fn deploy(f: &mut Fleet, id: &str, created_at: u64, receipt: Option<u8>) {
        let device = crate::chain::normalize("0xd1").unwrap();
        let tx = f.index.transaction().unwrap();
        store::put_deployment(&tx, &Deployment {
            id: crate::chain::normalize(id).unwrap(), creator: crate::chain::normalize("0x0a").unwrap(), component_name: "app".into(),
            version: "2.0.0".into(), recipe_blob_id: "r".into(), artifact_sha256: "a".repeat(64), recipe_sha256: None,
            created_at, schema_version: 1, catalog_version: None, deleted: false, target_groups: vec![], target_devices: vec![device.clone()],
        }, None).unwrap();
        if let Some(status) = receipt {
            store::put_receipts(&tx, &crate::chain::normalize(id).unwrap(), &[(device, status)], None).unwrap();
        }
        tx.commit().unwrap();
    }

    fn report(f: &Fleet, edit: impl FnOnce(&mut telemetry::Snapshot), received: u64) {
        let mut s = snapshot(received, received);
        edit(&mut s);
        let me = device(&address(&KEY));
        let verdict = telemetry::ingest(&f.telemetry, &binding(), 10, |_| Ok(Some(me.clone())), &sign(&s, &KEY), received).unwrap();
        assert_eq!(verdict.status, "accepted", "{verdict:?}");
    }

    fn kinds(alerts: &[Alert]) -> Vec<&'static str> {
        alerts.iter().map(|a| a.kind).collect()
    }

    #[test]
    fn alerts_follow_health_and_receipts() {
        let mut f = setup();
        let now = 10_000_000;
        // Healthy, no deployments: nothing.
        report(&f, |_| {}, now);
        let me = store::read_device(&f.index, &crate::chain::normalize("0xd1").unwrap()).unwrap().unwrap();
        assert!(for_device(&f.index, &f.telemetry, &me, now, DEFAULT_STUCK_AFTER_MS).unwrap().is_empty());
        // A failed receipt and an old unreported deployment on an online device.
        deploy(&mut f, "0xe1", now - 1000, Some(2));
        deploy(&mut f, "0xe2", now - DEFAULT_STUCK_AFTER_MS - 1, None);
        deploy(&mut f, "0xe3", now - 1000, None); // recent: not yet an alert
        deploy(&mut f, "0xe4", now - DEFAULT_STUCK_AFTER_MS - 1, Some(1)); // applied
        let alerts = for_device(&f.index, &f.telemetry, &me, now, DEFAULT_STUCK_AFTER_MS).unwrap();
        assert_eq!(kinds(&alerts), ["deployment-failed", "deployment-pending"]);
        assert!(alerts[0].action.contains("logs"));
        // A component past its restart budget makes the device degraded.
        report(&f, |s| {
            s.components[0].phase = Some("broken".into());
            s.components[0].last_exit = Some("exited with code 3; restart budget exhausted".into());
            s.health = "degraded".into();
            s.reasons = vec!["fw is broken".into()];
        }, now + 1);
        let alerts = for_device(&f.index, &f.telemetry, &me, now + 1, DEFAULT_STUCK_AFTER_MS).unwrap();
        assert_eq!(kinds(&alerts), ["component-broken", "device-degraded", "deployment-failed", "deployment-pending"]);
        assert_eq!(alerts[0].severity, "critical");
        assert!(alerts[0].message.contains("restart budget exhausted"));
        // Offline: no pending alert for a device that cannot report.
        let later = now + 1 + 4 * 60_000;
        let alerts = for_device(&f.index, &f.telemetry, &me, later, DEFAULT_STUCK_AFTER_MS).unwrap();
        assert_eq!(kinds(&alerts), ["device-offline", "deployment-failed"]);
        // A revoked device raises nothing (its telemetry is not trusted).
        let mut revoked = me.clone();
        revoked.revoked = true;
        assert!(for_device(&f.index, &f.telemetry, &revoked, later, DEFAULT_STUCK_AFTER_MS).unwrap().is_empty());
        // Fleet view: critical first.
        let all = fleet_alerts(&f, later);
        assert_eq!(all[0].severity, "critical");
    }

    #[test]
    fn service_health_raises_actionable_alerts_while_online() {
        let f = setup();
        let now = 1_120_000;
        report(&f, |s| {
            (s.schema, s.services) = (2, Some(crate::services::tests::services()));
            s.health = "degraded".into();
            s.reasons = vec!["MQTT bridge disconnected for 120 s: connection refused".into()];
        }, now);
        let me = store::read_device(&f.index, &crate::chain::normalize("0xd1").unwrap()).unwrap().unwrap();
        let alerts = for_device(&f.index, &f.telemetry, &me, now, DEFAULT_STUCK_AFTER_MS).unwrap();
        assert_eq!(kinds(&alerts), ["device-degraded", "messaging-backpressure", "messaging-loss", "bridge-disconnected", "stream-near-limit",
                                    "stream-data-lost", "export-failing", "shadow-conflict"]);
        let keys: std::collections::BTreeSet<_> = alerts.iter().map(|a| a.key.as_str()).collect();
        assert_eq!(keys.len(), alerts.len(), "stable, distinct keys");
        let failing = alerts.iter().find(|a| a.kind == "export-failing").unwrap();
        assert_eq!((failing.severity, failing.key.as_str()), ("critical", "export-failing:0x00000000000000000000000000000000000000000000000000000000000000d1:metrics"));
        assert!(failing.message.contains("500") && failing.action.contains("destination"));
        // The fleet view puts data loss and failing exports first.
        let all = fleet_alerts(&f, now);
        assert_eq!(all.iter().take(2).map(|a| a.kind).collect::<Vec<_>>(), ["export-failing", "stream-data-lost"]);
        // An offline device's last report raises no service alerts.
        let later = now + 4 * 60_000;
        assert_eq!(kinds(&for_device(&f.index, &f.telemetry, &me, later, DEFAULT_STUCK_AFTER_MS).unwrap()), ["device-offline"]);
    }

    fn fleet_alerts(f: &Fleet, now: u64) -> Vec<Alert> {
        super::fleet(&f.index, &f.telemetry, &Filter::default(), now, DEFAULT_STUCK_AFTER_MS).unwrap()
    }

    #[test]
    fn webhook_changes_send_each_alert_once_and_report_resolution() {
        let mut f = setup();
        let now = 10_000_000;
        report(&f, |_| {}, now);
        deploy(&mut f, "0xe1", now - 1000, Some(2));
        let current = fleet_alerts(&f, now);
        let (new, resolved) = changes(&f.telemetry, &current).unwrap();
        assert_eq!((new.len(), resolved.len()), (1, 0));
        commit(&f.telemetry, &new, &resolved, now).unwrap();
        let (new, resolved) = changes(&f.telemetry, &current).unwrap();
        assert!(new.is_empty() && resolved.is_empty(), "sent once");
        let (new, resolved) = changes(&f.telemetry, &[]).unwrap();
        assert_eq!((new.len(), resolved.clone()), (0, vec![current[0].key.clone()]));
        commit(&f.telemetry, &new, &resolved, now).unwrap();
        assert!(changes(&f.telemetry, &[]).unwrap().1.is_empty());
    }

    #[tokio::test]
    async fn webhook_delivers_once_and_retries_after_a_failure() {
        use std::sync::{Arc, Mutex};
        let dir = tempfile::tempdir().unwrap();
        let mut index = store::open(&dir.path().join("fleet.db")).unwrap();
        let tx = index.transaction().unwrap();
        store::put_device(&tx, &device(&address(&KEY)), None).unwrap();
        let id = crate::chain::normalize("0xe1").unwrap();
        let target = crate::chain::normalize("0xd1").unwrap();
        store::put_deployment(&tx, &Deployment {
            id: id.clone(), creator: crate::chain::normalize("0x0a").unwrap(), component_name: "app".into(), version: "2".into(),
            recipe_blob_id: "r".into(), artifact_sha256: "a".repeat(64), recipe_sha256: None, created_at: 1, schema_version: 1,
            catalog_version: None, deleted: false, target_groups: vec![], target_devices: vec![target.clone()],
        }, None).unwrap();
        store::put_receipts(&tx, &id, &[(target, 2)], None).unwrap();
        tx.commit().unwrap();
        let path = dir.path().join("telemetry.db");
        let writer = telemetry::open(&path, &binding(), false).unwrap();
        let service = telemetry::Service { path, binding: binding(), retain: 10, writer: std::sync::Mutex::new(writer) };
        let received: Arc<Mutex<Vec<serde_json::Value>>> = Arc::default();
        let failing = Arc::new(Mutex::new(true));
        let app = axum::Router::new().route("/hook", axum::routing::post({
            let (received, failing) = (received.clone(), failing.clone());
            move |axum::Json(body): axum::Json<serde_json::Value>| async move {
                if *failing.lock().unwrap() {
                    return axum::http::StatusCode::SERVICE_UNAVAILABLE;
                }
                received.lock().unwrap().push(body);
                axum::http::StatusCode::OK
            }
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = reqwest::Url::parse(&format!("http://{}/hook", listener.local_addr().unwrap())).unwrap();
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let readers = store::Readers::new(&dir.path().join("fleet.db"));
        let http = reqwest::Client::new();
        assert!(notify_once(&readers, &service, &http, &url, DEFAULT_STUCK_AFTER_MS).await.is_err(), "a failed delivery is an error");
        *failing.lock().unwrap() = false;
        assert_eq!(notify_once(&readers, &service, &http, &url, DEFAULT_STUCK_AFTER_MS).await.unwrap(), 1, "retried after the failure");
        assert_eq!(notify_once(&readers, &service, &http, &url, DEFAULT_STUCK_AFTER_MS).await.unwrap(), 0, "not sent twice");
        let bodies = received.lock().unwrap();
        assert_eq!(bodies.len(), 1);
        assert_eq!(bodies[0]["alerts"][0]["kind"], "deployment-failed");
    }
}
