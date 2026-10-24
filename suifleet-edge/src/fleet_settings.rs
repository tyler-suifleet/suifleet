//! Per-fleet cost settings (SF-130, docs/FLEET_SETTINGS.md).
//!
//! The fleet owner keeps one `fleet_settings::Settings` record in a dynamic
//! field of the fleet's `FleetBudget`. The daemon watches its version each check
//! and refreshes it before choosing when to check next, so a change takes effect
//! within one interval. Local configuration can only make the device slower
//! (cheaper), never faster than the fleet allows:
//!
//! - checks every `max(runtime.poll_seconds, check_interval_ms)`;
//! - where the SF-123 check cadence (`cadence.rs`) uses fast checks
//!   (rollout eligibility, admission, an open maintenance window), every
//!   `max(runtime.rollout_poll_seconds, rollout_check_interval_ms)`, or the
//!   routine interval when the fleet turned fast checks off (0). Single
//!   wakeups at a known admission or window start are kept;
//! - health and routine archives at the slower of the `fleet_data` cadence
//!   (the contract-enforced minimum) and the fleet's chosen cadence.
//!
//! A fleet without a record, or a deployment without the settings types,
//! leaves the local configuration in force. A record the daemon cannot use
//! (unreadable, out of bounds or a newer version) keeps the last applied one.
use serde::Serialize;
use crate::config::RuntimeConfig;
use serde_json::Value;
use crate::protocol::fleet_settings_values as bounds;

/// The record version this daemon understands.
pub const VERSION: u64 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Settings {
    pub version: u64,
    pub check_interval_ms: u64,
    pub rollout_check_interval_ms: u64,
    pub health_interval_ms: u64,
    pub archive_interval_ms: u64,
    pub archive_retention_epochs: u32,
}

impl Settings {
    /// The record as the node renders it (`dynamic_field::Field.value`).
    pub fn from_json(value: &Value) -> anyhow::Result<Self> {
        let number = |name: &str| crate::sui_client::json_u64(&value[name])
            .ok_or_else(|| anyhow::anyhow!("fleet settings field {name} is not an integer"));
        let settings = Self {
            version: number("version")?,
            check_interval_ms: number("check_interval_ms")?,
            rollout_check_interval_ms: number("rollout_check_interval_ms")?,
            health_interval_ms: number("health_interval_ms")?,
            archive_interval_ms: number("archive_interval_ms")?,
            archive_retention_epochs: u32::try_from(number("archive_retention_epochs")?)?,
        };
        settings.validate()?;
        Ok(settings)
    }

    /// The contract's bounds; a record outside them is never applied.
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.version == VERSION, "unsupported fleet settings version {}; update the daemon", self.version);
        anyhow::ensure!((bounds::MIN_CHECK_INTERVAL_MS..=bounds::MAX_CHECK_INTERVAL_MS).contains(&self.check_interval_ms),
            "fleet check interval out of bounds");
        anyhow::ensure!(self.rollout_check_interval_ms == 0
            || ((bounds::MIN_ROLLOUT_CHECK_INTERVAL_MS..=bounds::MAX_ROLLOUT_CHECK_INTERVAL_MS).contains(&self.rollout_check_interval_ms)
                && self.rollout_check_interval_ms <= self.check_interval_ms), "fleet rollout check interval out of bounds");
        anyhow::ensure!((bounds::MIN_HEALTH_INTERVAL_MS..=bounds::MAX_HEALTH_INTERVAL_MS).contains(&self.health_interval_ms),
            "fleet health interval out of bounds");
        anyhow::ensure!((bounds::MIN_ARCHIVE_INTERVAL_MS..=bounds::MAX_ARCHIVE_INTERVAL_MS).contains(&self.archive_interval_ms),
            "fleet archive interval out of bounds");
        anyhow::ensure!((bounds::MIN_ARCHIVE_RETENTION_EPOCHS..=bounds::MAX_ARCHIVE_RETENTION_EPOCHS).contains(&self.archive_retention_epochs),
            "fleet archive retention out of bounds");
        Ok(())
    }
}

/// The local runtime with the fleet's intervals as floors, for the check
/// cadence. Without a record the local configuration is unchanged.
pub fn runtime(local: &RuntimeConfig, settings: Option<&Settings>) -> RuntimeConfig {
    let mut runtime = local.clone();
    if let Some(s) = settings {
        runtime.poll_seconds = check_seconds(local.poll_seconds, s);
        runtime.rollout_poll_seconds = match s.rollout_check_interval_ms {
            0 => runtime.poll_seconds,
            fast => local.rollout_poll_seconds.max(fast.div_ceil(1000)),
        };
    }
    runtime
}

/// The routine check interval in seconds: never faster than the fleet's.
pub fn check_seconds(local_poll_seconds: u64, settings: &Settings) -> u64 {
    local_poll_seconds.max(settings.check_interval_ms.div_ceil(1000))
}

/// The slower of the contract-enforced `fleet_data` cadence and the fleet's choice.
pub fn health_interval_ms(fleet_data_ms: u64, settings: Option<&Settings>) -> u64 {
    settings.map_or(fleet_data_ms, |s| fleet_data_ms.max(s.health_interval_ms))
}
pub fn archive_interval_ms(fleet_data_ms: u64, settings: Option<&Settings>) -> u64 {
    settings.map_or(fleet_data_ms, |s| fleet_data_ms.max(s.archive_interval_ms))
}

/// The settings in force, updated from each check's read.
#[derive(Debug, Default)]
pub struct Applied {
    /// Which loop applies them, for the log.
    scope: &'static str,
    current: Option<Settings>,
    /// The last read failed or was unusable; logged once until it recovers.
    degraded: bool,
    generation: Option<u64>,
}

impl Applied {
    pub fn new(scope: &'static str) -> Self { Self { scope, ..Self::default() } }
    pub fn current(&self) -> Option<&Settings> { self.current.as_ref() }

    /// Settings and their absence are watched with the budget. Refresh only
    /// after invalidation; sleeping producers and idle checks do no extra reads.
    pub async fn refresh(&mut self, sui: &crate::sui_client::SuiClient, budget: &str) {
        let generation = sui.watch_generation();
        if self.generation == Some(generation) { return; }
        let result = read(sui, budget).await;
        if result.is_ok() { self.generation = Some(generation); }
        self.update(result);
    }

    /// Apply one read: a record, its absence, or an error. Returns true when
    /// the settings in force changed.
    pub fn update(&mut self, read: anyhow::Result<Option<Settings>>) -> bool {
        match read {
            Ok(next) => {
                if self.degraded { tracing::info!(scope = self.scope, "Fleet settings readable again"); }
                self.degraded = false;
                if next == self.current { return false; }
                match &next {
                    Some(s) => tracing::info!(scope = self.scope, check_interval_ms = s.check_interval_ms,
                        rollout_check_interval_ms = s.rollout_check_interval_ms,
                        health_interval_ms = s.health_interval_ms, archive_interval_ms = s.archive_interval_ms,
                        archive_retention_epochs = s.archive_retention_epochs, "Fleet settings applied"),
                    None => tracing::info!(scope = self.scope, "No fleet settings record; local configuration in force"),
                }
                self.current = next;
                true
            }
            Err(error) => {
                if !self.degraded {
                    tracing::warn!(scope = self.scope, error = %format!("{error:#}"), "Fleet settings unusable; keeping the settings in force");
                }
                self.degraded = true;
                false
            }
        }
    }
}

/// The fleet budget whose settings this device follows: its `[budget]`, else
/// its `[fleet_data]` budget (configuration validation requires them to agree).
pub fn budget_id(cfg: &crate::config::Config) -> Option<&str> {
    cfg.budget.as_ref().map(|b| b.budget_id.as_str())
        .or_else(|| cfg.fleet_data.as_ref().map(|d| d.budget_id.as_str()))
}

/// Read the fleet's record: None when the deployment predates the settings
/// types or the owner has not created one.
pub async fn read(sui: &crate::sui_client::SuiClient, budget_id: &str) -> anyhow::Result<Option<Settings>> {
    match sui.fleet_settings(budget_id).await? {
        Some(value) => Settings::from_json(&value).map(Some),
        None => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::{Control, Deferred};

    fn defaults() -> Settings {
        Settings { version: 1, check_interval_ms: bounds::DEFAULT_CHECK_INTERVAL_MS,
            rollout_check_interval_ms: bounds::DEFAULT_ROLLOUT_CHECK_INTERVAL_MS,
            health_interval_ms: bounds::DEFAULT_HEALTH_INTERVAL_MS, archive_interval_ms: bounds::DEFAULT_ARCHIVE_INTERVAL_MS,
            archive_retention_epochs: bounds::DEFAULT_ARCHIVE_RETENTION_EPOCHS }
    }

    #[test]
    fn defaults_match_the_business_plan_and_bounds() {
        let d = defaults();
        assert_eq!((d.check_interval_ms, d.rollout_check_interval_ms, d.health_interval_ms, d.archive_interval_ms, d.archive_retention_epochs),
            (30 * 60_000, 60_000, 86_400_000, 7 * 86_400_000, 3));
        d.validate().unwrap();
    }

    #[test]
    fn parses_node_json_and_rejects_records_outside_the_contract() {
        let json = serde_json::json!({"version": "1", "check_interval_ms": "1800000", "rollout_check_interval_ms": "60000",
            "health_interval_ms": "86400000", "archive_interval_ms": "604800000", "archive_retention_epochs": 3});
        assert_eq!(Settings::from_json(&json).unwrap(), defaults());
        let edit = |f: &dyn Fn(&mut Settings)| { let mut s = defaults(); f(&mut s); s.validate() };
        assert!(edit(&|s| s.version = 2).is_err(), "newer record");
        assert!(edit(&|s| s.check_interval_ms = bounds::MIN_CHECK_INTERVAL_MS - 1).is_err());
        assert!(edit(&|s| s.check_interval_ms = bounds::MAX_CHECK_INTERVAL_MS + 1).is_err());
        assert!(edit(&|s| s.rollout_check_interval_ms = bounds::MIN_ROLLOUT_CHECK_INTERVAL_MS - 1).is_err());
        assert!(edit(&|s| { s.check_interval_ms = 300_000; s.rollout_check_interval_ms = 300_001 }).is_err(), "fast slower than routine");
        assert!(edit(&|s| s.rollout_check_interval_ms = 0).is_ok(), "fast checks off");
        assert!(edit(&|s| s.health_interval_ms = bounds::MAX_HEALTH_INTERVAL_MS + 1).is_err());
        assert!(edit(&|s| s.archive_interval_ms = bounds::MIN_ARCHIVE_INTERVAL_MS - 1).is_err());
        assert!(edit(&|s| s.archive_retention_epochs = 0).is_err());
        assert!(edit(&|s| s.archive_retention_epochs = bounds::MAX_ARCHIVE_RETENTION_EPOCHS + 1).is_err());
        let mut missing = json.clone(); missing.as_object_mut().unwrap().remove("health_interval_ms");
        assert!(Settings::from_json(&missing).is_err());
        let mut fractional = json; fractional["check_interval_ms"] = serde_json::json!(1800000.5);
        assert!(Settings::from_json(&fractional).is_err());
    }

    #[test]
    fn local_configuration_only_slows_the_device() {
        let s = defaults();
        let local = |poll_seconds, rollout_poll_seconds| RuntimeConfig { poll_seconds, rollout_poll_seconds, ..RuntimeConfig::default() };
        let intervals = |r: RuntimeConfig| (r.poll_seconds, r.rollout_poll_seconds);
        // Faster local polling never exceeds the fleet's cadence.
        assert_eq!(intervals(runtime(&local(10, 1), Some(&s))), (1800, 60));
        // A slower local cadence wins for both.
        assert_eq!(intervals(runtime(&local(3600, 120), Some(&s))), (3600, 120));
        // Fast checks off: the routine interval everywhere.
        assert_eq!(intervals(runtime(&local(10, 1), Some(&Settings { rollout_check_interval_ms: 0, ..s }))), (1800, 1800));
        // Sub-second record values round up, never down.
        let odd = Settings { check_interval_ms: 300_001, rollout_check_interval_ms: 30_500, ..s };
        assert_eq!(intervals(runtime(&local(1, 1), Some(&odd))), (301, 31));
        // No record: the local configuration.
        assert_eq!(intervals(runtime(&local(10, 1), None)), (10, 1));
        // The check cadence uses the bounded values.
        let cadence = crate::cadence::Cadence::default();
        assert_eq!(cadence.delay(&runtime(&local(1, 1), Some(&s)), [].iter(), 0, None).as_secs(), 1800);
        let waiting = Deferred { control: Control::default(), notified_ms: 0, reason: "waiting for rollout eligibility".into(), terminal: false };
        assert_eq!(cadence.delay(&runtime(&local(1, 1), Some(&s)), [&waiting].into_iter(), 0, None).as_secs(), 60);
        assert_eq!(cadence.delay(&runtime(&local(1, 1), Some(&Settings { rollout_check_interval_ms: 0, ..s })),
            [&waiting].into_iter(), 0, None).as_secs(), 1800);
        // Health and archives: the fleet_data floor still applies.
        assert_eq!(health_interval_ms(3_600_000, Some(&s)), 86_400_000);
        assert_eq!(health_interval_ms(3_600_000, Some(&Settings { health_interval_ms: 60_000, ..s })), 3_600_000);
        assert_eq!(health_interval_ms(3_600_000, None), 3_600_000);
        assert_eq!(archive_interval_ms(86_400_000, Some(&s)), 604_800_000);
        assert_eq!(archive_interval_ms(86_400_000, None), 86_400_000);
    }

    #[test]
    fn unusable_reads_keep_the_settings_in_force() {
        let mut applied = Applied::default();
        assert!(!applied.update(Ok(None)), "absent stays absent");
        assert!(applied.update(Ok(Some(defaults()))));
        assert!(!applied.update(Ok(Some(defaults()))), "unchanged");
        assert!(!applied.update(Err(anyhow::anyhow!("node unavailable"))));
        assert_eq!(applied.current(), Some(&defaults()));
        let next = Settings { check_interval_ms: 300_000, ..defaults() };
        assert!(applied.update(Ok(Some(next))));
        assert_eq!(applied.current(), Some(&next));
        // A record that is no longer present returns to the local configuration.
        assert!(applied.update(Ok(None)));
        assert_eq!(applied.current(), None);
    }
}
