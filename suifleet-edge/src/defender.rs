//! Device Defender 4.0.0 device-side adapter; JSON metric schema 1.0.
//! No Detect/security-profile evaluation is performed here. See DEVICE_DEFENDER.md.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

pub const SCHEMA: &str = "1.0";
const MAX_REPORT: usize = 64 * 1024;
const MAX_PROC: u64 = 4 * 1024 * 1024;
fn interval() -> u64 {
    300
}
fn retries() -> u32 {
    5
}
fn buffer() -> usize {
    12
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    #[serde(default = "interval")]
    pub sample_interval_seconds: u64,
    #[serde(default = "retries")]
    pub publish_retry_count: u32,
    #[serde(default = "buffer")]
    pub buffer_max: usize,
}
impl Settings {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(
            (300..=86400).contains(&self.sample_interval_seconds),
            "device_defender.sample_interval_seconds must be 300..=86400"
        );
        anyhow::ensure!(
            self.publish_retry_count <= 72,
            "device_defender.publish_retry_count must be 0..=72"
        );
        anyhow::ensure!(
            (1..=128).contains(&self.buffer_max),
            "device_defender.buffer_max must be 1..=128"
        );
        Ok(())
    }
}
pub fn topic(thing: &str) -> String {
    format!("$aws/things/{thing}/defender/metrics/json")
}
pub fn filters(thing: &str) -> Vec<String> {
    ["accepted", "rejected"]
        .map(|outcome| format!("{}/{outcome}", topic(thing)))
        .to_vec()
}
fn read(path: &Path, max: u64) -> anyhow::Result<String> {
    let mut text = String::new();
    std::fs::File::open(path)?
        .take(max + 1)
        .read_to_string(&mut text)?;
    anyhow::ensure!(
        text.len() as u64 <= max,
        "metrics input exceeds bounded read: {}",
        path.display()
    );
    Ok(text)
}

#[derive(Default, Debug, Clone, Serialize, Deserialize)]
struct Counters {
    bytes_in: u64,
    bytes_out: u64,
    packets_in: u64,
    packets_out: u64,
}
impl Counters {
    fn add_delta(&mut self, now: &Self, old: &Self) {
        // Reset/wrap is not a negative count or traffic since the last boot.
        self.bytes_in = self
            .bytes_in
            .saturating_add(now.bytes_in.saturating_sub(old.bytes_in));
        self.bytes_out = self
            .bytes_out
            .saturating_add(now.bytes_out.saturating_sub(old.bytes_out));
        self.packets_in = self
            .packets_in
            .saturating_add(now.packets_in.saturating_sub(old.packets_in));
        self.packets_out = self
            .packets_out
            .saturating_add(now.packets_out.saturating_sub(old.packets_out));
    }
}
#[derive(Default, Debug, Clone, Serialize, Deserialize)]
struct Baseline {
    boot: String,
    interfaces: BTreeMap<String, Counters>,
}
fn network(proc: &Path) -> anyhow::Result<Baseline> {
    let boot = read(&proc.join("sys/kernel/random/boot_id"), 128)?
        .trim()
        .to_owned();
    let mut interfaces = BTreeMap::new();
    for line in read(&proc.join("net/dev"), MAX_PROC)?.lines().skip(2) {
        let (name, fields) = line
            .split_once(':')
            .ok_or_else(|| anyhow::anyhow!("invalid net/dev row"))?;
        let fields: Vec<u64> = fields
            .split_whitespace()
            .map(str::parse)
            .collect::<Result<_, _>>()?;
        anyhow::ensure!(
            fields.len() == 16 && interfaces.len() < 1024 && name.trim().len() <= 64,
            "invalid/oversize net/dev table"
        );
        interfaces.insert(
            name.trim().into(),
            Counters {
                bytes_in: fields[0],
                packets_in: fields[1],
                bytes_out: fields[8],
                packets_out: fields[9],
            },
        );
    }
    Ok(Baseline { boot, interfaces })
}
fn address(text: &str) -> anyhow::Result<(String, u16)> {
    let (host, port) = text
        .split_once(':')
        .ok_or_else(|| anyhow::anyhow!("invalid socket address"))?;
    let port = u16::from_str_radix(port, 16)?;
    let ip = match host.len() {
        8 => std::net::IpAddr::V4(std::net::Ipv4Addr::from(
            u32::from_str_radix(host, 16)?.to_ne_bytes(),
        )),
        32 => {
            let mut bytes = [0u8; 16];
            for (i, word) in host.as_bytes().chunks_exact(8).enumerate() {
                bytes[i * 4..i * 4 + 4].copy_from_slice(
                    &u32::from_str_radix(std::str::from_utf8(word)?, 16)?.to_ne_bytes(),
                );
            }
            std::net::IpAddr::V6(std::net::Ipv6Addr::from(bytes))
        }
        _ => anyhow::bail!("invalid socket IP"),
    };
    Ok((std::net::SocketAddr::new(ip, port).to_string(), port))
}
/// Read only the daemon's network namespace. Never invent an interface name
/// from an IP binding, and never silently truncate an oversize socket table.
fn sockets(proc: &Path, tcp: bool) -> anyhow::Result<(Value, Vec<Value>)> {
    let mut ports = BTreeSet::new();
    let mut connections = Vec::new();
    let protocol = if tcp { "tcp" } else { "udp" };
    let mut rows = 0;
    for suffix in ["", "6"] {
        let path = proc.join(format!("net/{protocol}{suffix}"));
        let text = match read(&path, MAX_PROC) {
            Ok(text) => text,
            Err(e)
                if suffix == "6"
                    && e.downcast_ref::<std::io::Error>()
                        .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
            {
                continue
            }
            Err(e) => return Err(e),
        };
        for row in text.lines().skip(1) {
            rows += 1;
            anyhow::ensure!(rows <= 4096, "socket table exceeds 4096 rows");
            let f: Vec<&str> = row.split_whitespace().collect();
            anyhow::ensure!(f.len() >= 4, "invalid socket table row");
            let (_, local_port) = address(f[1])?;
            if (tcp && f[3] == "0A") || (!tcp && f[3] == "07") {
                if local_port != 0 {
                    ports.insert(local_port);
                }
            }
            if tcp && f[3] == "01" {
                let (remote, _) = address(f[2])?;
                connections.push(json!({"local_port": local_port, "remote_addr": remote}));
            }
        }
    }
    let list: Vec<Value> = ports.iter().map(|port| json!({"port": port})).collect();
    Ok((json!({"ports": list, "total": list.len()}), connections))
}
fn sample(proc: &Path, old: Option<&Baseline>, id: u64) -> anyhow::Result<(Value, Baseline)> {
    let current = network(proc)?;
    let (tcp, connections) = sockets(proc, true)?;
    let (udp, _) = sockets(proc, false)?;
    let mut metrics = json!({"listening_tcp_ports": tcp, "listening_udp_ports": udp,
        "tcp_connections": {"established_connections": {"total": connections.len(), "connections": connections}}});
    // First sample, reboot, or clock rollback starts a new network baseline.
    if let Some(old) = old.filter(|b| b.boot == current.boot) {
        let mut delta = Counters::default();
        for (name, now) in &current.interfaces {
            if let Some(before) = old.interfaces.get(name) {
                delta.add_delta(now, before);
            }
        }
        metrics["network_stats"] = serde_json::to_value(delta)?;
    }
    let report = json!({"header": {"report_id": id, "version": SCHEMA}, "metrics": metrics});
    anyhow::ensure!(
        serde_json::to_vec(&report)?.len() <= MAX_REPORT,
        "Device Defender report exceeds 64 KiB; sample refused"
    );
    Ok((report, current))
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Pending {
    report: Value,
    attempts: u32,
    retry_at_ms: u64,
}
#[derive(Default, Debug, Serialize, Deserialize)]
struct State {
    thing: String,
    last_id: u64,
    last_sample_ms: u64,
    baseline: Option<Baseline>,
    queue: VecDeque<Pending>,
    accepted: u64,
    rejected: u64,
    dropped: u64,
    last_error: Option<String>,
}
pub struct Exporter {
    settings: Settings,
    path: PathBuf,
    state: State,
}
impl Exporter {
    pub fn open(settings: Settings, dir: PathBuf, thing: &str) -> anyhow::Result<Self> {
        settings.validate()?;
        std::fs::create_dir_all(&dir)?;
        let path = dir.join("state.json");
        let mut state: State = match read(&path, 9 * 1024 * 1024) {
            Ok(text) => serde_json::from_str(&text)?,
            Err(e)
                if e.downcast_ref::<std::io::Error>()
                    .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
            {
                State::default()
            }
            Err(e) => return Err(e),
        };
        anyhow::ensure!(state.thing.is_empty() || state.thing == thing, "Device Defender spool belongs to a different thing; archive it explicitly before changing identity");
        anyhow::ensure!(
            state.queue.len() <= 128,
            "Device Defender spool exceeds queue bound"
        );
        for pending in &state.queue {
            anyhow::ensure!(
                pending.report["header"]["version"] == SCHEMA
                    && serde_json::to_vec(&pending.report)?.len() <= MAX_REPORT,
                "unsupported/oversize persisted Defender report"
            );
        }
        state.thing = thing.into();
        while state.queue.len() > settings.buffer_max {
            state.queue.pop_front();
            state.dropped += 1;
        }
        let exporter = Self {
            settings,
            path,
            state,
        };
        exporter.save()?;
        Ok(exporter)
    }
    fn save(&self) -> anyhow::Result<()> {
        let tmp = self.path.with_extension("tmp");
        let mut options = std::fs::OpenOptions::new();
        options.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&tmp)?;
        file.write_all(&serde_json::to_vec(&self.state)?)?;
        file.sync_all()?;
        std::fs::rename(tmp, &self.path)?;
        std::fs::File::open(self.path.parent().unwrap())?.sync_all()?;
        Ok(())
    }
    fn collect(&mut self, proc: &Path, now: u64) -> anyhow::Result<()> {
        if now.saturating_sub(self.state.last_sample_ms)
            < self.settings.sample_interval_seconds * 1000
        {
            return Ok(());
        }
        let id = now.max(
            self.state
                .last_id
                .checked_add(1)
                .ok_or_else(|| anyhow::anyhow!("Defender report id exhausted"))?,
        );
        let (report, baseline) = sample(proc, self.state.baseline.as_ref(), id)?;
        self.state.last_id = id;
        self.state.last_sample_ms = now;
        self.state.baseline = Some(baseline);
        if self.state.queue.len() == self.settings.buffer_max {
            self.state.queue.pop_front();
            self.state.dropped += 1;
        }
        self.state.queue.push_back(Pending {
            report,
            attempts: 0,
            retry_at_ms: now,
        });
        self.save()
    }
    async fn send(
        &mut self,
        client: &crate::iotcore::ShadowClient,
        now: u64,
    ) -> anyhow::Result<()> {
        if !client.is_connected() {
            return Ok(());
        }
        let Some(pending) = self.state.queue.front_mut() else {
            return Ok(());
        };
        if pending.retry_at_ms > now {
            return Ok(());
        }
        if pending.attempts > self.settings.publish_retry_count {
            self.state.queue.pop_front();
            self.state.dropped += 1;
            self.state.last_error =
                Some("report retry allowance exhausted (delivery unknown)".into());
            return self.save();
        }
        pending.attempts += 1;
        pending.retry_at_ms =
            now.saturating_add((5u64.saturating_mul(1 << pending.attempts.min(6))).min(300) * 1000);
        let report = pending.report.clone();
        // Persist the allowance BEFORE writing to the network; crashes cannot
        // reset the counter. Replays use exactly the same report id and bytes.
        self.save()?;
        match client.defender(&report).await {
            Ok((accepted, value)) => {
                self.state.queue.pop_front();
                if accepted {
                    self.state.accepted += 1;
                    self.state.last_error = None;
                } else {
                    self.state.rejected += 1;
                    // Keep service-provided text bounded and redacted; never log metrics/IPs.
                    self.state.last_error = Some(
                        crate::redact::redact(
                            &crate::telemetry::bounded(&value["statusDetails"].to_string(), 200),
                            &[],
                        )
                        .0,
                    );
                }
            }
            Err(error) => {
                self.state.last_error = Some(crate::telemetry::bounded(&error.to_string(), 200))
            }
        }
        self.save()
    }
    pub async fn run(mut self, client: crate::iotcore::ShadowClient) {
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tick.tick().await;
            let now = crate::telemetry::now_ms();
            // A storage failure stops the task rather than performing unjournaled sends.
            if let Err(error) = self.collect(Path::new("/proc"), now) {
                tracing::warn!(%error, "Device Defender sampling failed");
                // Sampling failures retry at the configured interval, not every tick.
                self.state.last_sample_ms = now;
                self.state.last_error = Some(crate::telemetry::bounded(&error.to_string(), 200));
                if self.save().is_err() {
                    return;
                }
            }
            if let Err(error) = self.send(&client, now).await {
                tracing::error!(%error, "Device Defender storage failed; exporter stopped");
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    fn dir() -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "sf062-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&path).unwrap();
        path
    }
    fn proc_fixture(path: &Path, value: u64) {
        std::fs::create_dir_all(path.join("net")).unwrap();
        std::fs::create_dir_all(path.join("sys/kernel/random")).unwrap();
        std::fs::write(path.join("sys/kernel/random/boot_id"), "boot-1").unwrap();
        std::fs::write(
            path.join("net/dev"),
            format!("header\nheader\neth0: {value} 20 0 0 0 0 0 0 200 30 0 0 0 0 0 0\n"),
        )
        .unwrap();
        std::fs::write(
            path.join("net/tcp"),
            "header\n0: 00000000:0016 00000000:0000 0A\n1: 0100007F:1F90 0200007F:01BB 01\n",
        )
        .unwrap();
        std::fs::write(
            path.join("net/udp"),
            "header\n0: 00000000:0035 00000000:0000 07\n",
        )
        .unwrap();
    }
    fn settings() -> Settings {
        serde_json::from_value(json!({})).unwrap()
    }
    #[test]
    fn settings_bounds_and_unknown_fields() {
        for (field, good, bad) in [
            (
                "sample_interval_seconds",
                vec![300, 86400],
                vec![0, 299, 86401],
            ),
            ("publish_retry_count", vec![0, 72], vec![73]),
            ("buffer_max", vec![1, 128], vec![0, 129]),
        ] {
            for value in good {
                assert!(serde_json::from_value::<Settings>(json!({field: value}))
                    .unwrap()
                    .validate()
                    .is_ok());
            }
            for value in bad {
                assert!(serde_json::from_value::<Settings>(json!({field: value}))
                    .unwrap()
                    .validate()
                    .is_err());
            }
        }
        assert!(serde_json::from_value::<Settings>(json!({"thing_name":"other"})).is_err());
    }
    #[test]
    fn schema_topic_fixture_and_counter_resets() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../interfaces/compat/device-defender-cases.json"
        ))
        .unwrap();
        assert_eq!(topic("thing1"), fixture["topic"]);
        assert_eq!(
            serde_json::to_value(filters("thing1")).unwrap(),
            fixture["filters"]
        );
        let dir = dir();
        proc_fixture(&dir, 100);
        let (first, baseline) = sample(&dir, None, 1).unwrap();
        assert!(first["metrics"].get("network_stats").is_none());
        proc_fixture(&dir, 130);
        let (second, baseline2) = sample(&dir, Some(&baseline), 2).unwrap();
        assert_eq!(second, fixture["report"]);
        proc_fixture(&dir, 5);
        let (reset, _) = sample(&dir, Some(&baseline2), 3).unwrap();
        assert_eq!(reset["metrics"]["network_stats"]["bytes_in"], 0);
        std::fs::write(dir.join("sys/kernel/random/boot_id"), "boot-2").unwrap();
        assert!(sample(&dir, Some(&baseline2), 4).unwrap().0["metrics"]
            .get("network_stats")
            .is_none());
        std::fs::write(dir.join("net/tcp"), "header\nmalformed\n").unwrap();
        assert!(sample(&dir, None, 5).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn sampling_spool_overflow_restart_identity_and_limits() {
        let dir = dir();
        let proc = dir.join("proc");
        proc_fixture(&proc, 100);
        let mut cfg = settings();
        cfg.buffer_max = 2;
        let spool = dir.join("spool");
        let mut exporter = Exporter::open(cfg.clone(), spool.clone(), "thing1").unwrap();
        exporter.collect(&proc, 300_000).unwrap();
        exporter.collect(&proc, 599_999).unwrap();
        assert_eq!(exporter.state.queue.len(), 1);
        exporter.collect(&proc, 600_000).unwrap();
        exporter.collect(&proc, 900_000).unwrap();
        assert_eq!(exporter.state.dropped, 1);
        assert_eq!(exporter.state.queue.len(), 2);
        exporter.state.queue[0].attempts = 3;
        exporter.save().unwrap();
        drop(exporter);
        let mut exporter = Exporter::open(cfg.clone(), spool.clone(), "thing1").unwrap();
        assert_eq!(exporter.state.queue[0].attempts, 3);
        exporter.collect(&proc, 1).unwrap(); // clock rollback never resets report ids
        assert_eq!(exporter.state.last_id, 900_000);
        assert!(Exporter::open(cfg, spool, "other").is_err());
        std::fs::write(proc.join("net/dev"), "x".repeat(MAX_PROC as usize + 1)).unwrap();
        assert!(exporter.collect(&proc, 1_200_000).is_err());
        assert_eq!(exporter.state.queue.len(), 2);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[tokio::test]
    async fn disconnected_queue_does_not_spend_retry_allowance() {
        let dir = dir();
        let proc = dir.join("proc");
        proc_fixture(&proc, 100);
        let mut exporter = Exporter::open(settings(), dir.join("spool"), "thing1").unwrap();
        exporter.collect(&proc, 300_000).unwrap();
        let (client, _endpoint) = crate::iotcore::shadow_channel("thing1", &[]);
        for _ in 0..100 {
            exporter.send(&client, 300_000).await.unwrap();
        }
        assert_eq!(exporter.state.queue[0].attempts, 0);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn oversize_report_is_refused_without_truncating_socket_inventory() {
        let dir = dir();
        proc_fixture(&dir, 100);
        let rows = "0: 0100007F:1F90 0200007F:01BB 01\n".repeat(4096);
        std::fs::write(dir.join("net/tcp"), format!("header\n{rows}")).unwrap();
        assert!(sample(&dir, None, 1)
            .unwrap_err()
            .to_string()
            .contains("64 KiB"));
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn ipv4_ipv6_and_real_procfs() {
        assert_eq!(
            address("0100007F:0016").unwrap(),
            ("127.0.0.1:22".into(), 22)
        );
        assert_eq!(
            address("00000000000000000000000001000000:01BB").unwrap(),
            ("[::1]:443".into(), 443)
        );
        #[cfg(target_os = "linux")]
        {
            sample(Path::new("/proc"), None, 1).unwrap();
        }
    }
}
