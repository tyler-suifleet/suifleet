//! Gas top-ups from the owner's prepaid fleet budget (SF-083,
//! docs/FLEET_BUDGET.md).
//!
//! The owner's `fleet_budget::FleetBudget` funds this device's address. When
//! the device's SUI balance falls below `low_water_mist`, the daemon draws
//! from the budget with one `fleet_budget::draw_gas` call, which the chain
//! bounds per draw, per period and by rate. The daemon reads its account
//! first and only draws when the contract would accept it, so a refused draw
//! costs nothing; it then records why it cannot draw (paused, suspended,
//! allowance used up, budget empty) in the journal, where telemetry reports
//! it. Status reports stay queued in the journal meanwhile: nothing is
//! dropped, and they go out once a later draw or owner refill succeeds.
use serde::{Deserialize, Serialize};
use serde_json::Value;

fn default_low_water() -> u64 {
    20_000_000
}
fn default_draw() -> u64 {
    50_000_000
}

/// `[budget]` in `config.toml`. The on-chain limits bound every draw; these
/// only choose when and how much to ask for.
#[derive(Deserialize, Serialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct BudgetConfig {
    /// The owner's shared `FleetBudget` object.
    pub budget_id: String,
    /// Draw when the device's SUI balance is below this (MIST). Keep it above
    /// the gas of one draw: a device with no gas cannot draw, and needs an
    /// owner refill (`fleet_budget::fund_device`).
    #[serde(default = "default_low_water")]
    pub low_water_mist: u64,
    /// Asked for per draw (MIST); the float cap, period allowance and budget
    /// balance may lower it.
    #[serde(default = "default_draw")]
    pub draw_mist: u64,
}

impl BudgetConfig {
    pub fn validate(&self, manifest: &crate::config::DeploymentManifest) -> anyhow::Result<()> {
        anyhow::ensure!(
            crate::sui_client::same_id(&self.budget_id, &self.budget_id),
            "budget.budget_id is not a Sui object ID"
        );
        anyhow::ensure!(self.draw_mist > 0, "budget.draw_mist must be positive");
        anyhow::ensure!(
            crate::protocol::BUDGET_TYPES.iter().all(|name| manifest.type_origins.contains_key(*name)),
            "[budget] needs a deployment manifest with the fleet budget type origins"
        );
        Ok(())
    }
}

/// Journal record of the device's funding.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize, Serialize)]
pub struct Funding {
    /// Why the device cannot top up now; none while it can (or need not).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub blocked: Option<String>,
    /// When `blocked` was first recorded (device clock, Unix ms).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub blocked_since_ms: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub balance_mist: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_draw_digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_draw_mist: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_draw_ms: Option<u64>,
    #[serde(default)]
    pub draws: u64,
}

impl Funding {
    pub fn block(&mut self, reason: String, now_ms: u64) {
        if self.blocked.as_deref() != Some(reason.as_str()) {
            self.blocked_since_ms = Some(now_ms);
        }
        self.blocked = Some(reason);
    }
    pub fn clear(&mut self) {
        self.blocked = None;
        self.blocked_since_ms = None;
    }
}

/// The budget fields a draw depends on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Budget {
    pub version: u64,
    pub paused: bool,
    pub balance: u64,
    pub registry_id: String,
}

/// The device's `fleet_budget::DeviceAccount`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Account {
    pub suspended: bool,
    pub float_cap: u64,
    pub period_ms: u64,
    pub period_allowance: u64,
    pub max_draws_per_period: u64,
    pub min_draw_interval_ms: u64,
    pub period_start_ms: u64,
    pub drawn_in_period: u64,
    pub draws_in_period: u64,
    pub last_draw_ms: u64,
}

/// A u64 the node renders as a JSON string or number.
fn u64_of(value: &Value) -> anyhow::Result<u64> {
    value
        .as_str()
        .and_then(|s| s.parse().ok())
        .or_else(|| value.as_u64())
        .or_else(|| value.as_f64().filter(|f| f.fract() == 0.0 && *f >= 0.0 && *f < 9_007_199_254_740_992.0).map(|f| f as u64))
        .ok_or_else(|| anyhow::anyhow!("not a u64: {value}"))
}

fn bool_of(value: &Value) -> anyhow::Result<bool> {
    value.as_bool().ok_or_else(|| anyhow::anyhow!("not a bool: {value}"))
}

impl Budget {
    /// From the node's JSON rendering of a `FleetBudget`. `Balance<SUI>` is
    /// accepted as its value or as `{ "value": ... }`.
    pub fn from_json(fields: &Value) -> anyhow::Result<Self> {
        let balance = &fields["balance"];
        let balance = if balance.is_object() { &balance["value"] } else { balance };
        Ok(Self {
            version: u64_of(&fields["version"])?,
            paused: bool_of(&fields["paused"])?,
            balance: u64_of(balance)?,
            registry_id: fields["registry_id"].as_str().unwrap_or_default().to_owned(),
        })
    }
}

impl Account {
    pub fn from_json(value: &Value) -> anyhow::Result<Self> {
        let limits = &value["limits"];
        Ok(Self {
            suspended: bool_of(&value["suspended"])?,
            float_cap: u64_of(&limits["float_cap"])?,
            period_ms: u64_of(&limits["period_ms"])?,
            period_allowance: u64_of(&limits["period_allowance"])?,
            max_draws_per_period: u64_of(&limits["max_draws_per_period"])?,
            min_draw_interval_ms: u64_of(&limits["min_draw_interval_ms"])?,
            period_start_ms: u64_of(&value["period_start_ms"])?,
            drawn_in_period: u64_of(&value["drawn_in_period"])?,
            draws_in_period: u64_of(&value["draws_in_period"])?,
            last_draw_ms: u64_of(&value["last_draw_ms"])?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// The balance is at or above the low-water mark.
    Enough,
    Draw(u64),
    /// The contract would refuse a draw now, and why.
    Blocked(String),
}

/// Whether, and how much, to draw: the same checks `fleet_budget::draw_gas`
/// makes, so a refused draw is never submitted. `now_ms` approximates the
/// chain clock; a disagreement only makes the draw's simulation fail.
pub fn decide(config: &BudgetConfig, balance: u64, budget: &Budget, account: Option<&Account>, now_ms: u64) -> Decision {
    if balance >= config.low_water_mist {
        return Decision::Enough;
    }
    if budget.paused {
        return Decision::Blocked("the fleet budget is paused by its owner".into());
    }
    let Some(account) = account else {
        return Decision::Blocked("this device is not enrolled in the fleet budget".into());
    };
    if account.suspended {
        return Decision::Blocked("this device is suspended in the fleet budget".into());
    }
    let (mut drawn, mut draws) = (account.drawn_in_period, account.draws_in_period);
    let period_end = account.period_start_ms.saturating_add(account.period_ms);
    if now_ms >= period_end {
        (drawn, draws) = (0, 0);
    }
    if draws >= account.max_draws_per_period {
        return Decision::Blocked(format!("draw limit for this period reached; the period ends at {period_end} ms"));
    }
    let next = account.last_draw_ms.saturating_add(account.min_draw_interval_ms);
    if account.last_draw_ms != 0 && now_ms < next {
        return Decision::Blocked(format!("draws are rate limited until {next} ms"));
    }
    let room = account.period_allowance.saturating_sub(drawn);
    if room == 0 {
        return Decision::Blocked(format!("gas allowance for this period used up; the period ends at {period_end} ms"));
    }
    if budget.balance == 0 {
        return Decision::Blocked("the fleet budget is empty; its owner must deposit".into());
    }
    let amount = config.draw_mist.min(account.float_cap).min(room).min(budget.balance);
    if amount == 0 {
        return Decision::Blocked("the float cap is zero".into());
    }
    Decision::Draw(amount)
}

/// One top-up check: read the balance, the budget and this device's account;
/// draw if the contract would allow it, otherwise record why not. Returns
/// whether `funding` changed. Errors (node unavailable, failed execution)
/// leave the previous record and are retried next poll.
pub async fn top_up(
    config: &BudgetConfig,
    sui: &crate::sui_client::SuiClient,
    device_id: &str,
    cap: &str,
    funding: &mut Funding,
    now_ms: u64,
) -> anyhow::Result<bool> {
    let before = funding.clone();
    let balance = sui.gas_balance().await?;
    funding.balance_mist = Some(balance);
    let (budget, account) = sui.budget(&config.budget_id, device_id).await?;
    match decide(config, balance, &budget, account.as_ref(), now_ms) {
        Decision::Enough => funding.clear(),
        Decision::Blocked(reason) => {
            tracing::warn!(%reason, balance, "Gas top-up from the fleet budget is not possible");
            funding.block(reason, now_ms);
        }
        Decision::Draw(amount) => {
            let digest = sui.draw_gas(&config.budget_id, cap, amount).await?;
            tracing::info!(%digest, amount, "Drew gas from the fleet budget");
            funding.clear();
            funding.balance_mist = Some(balance.saturating_add(amount));
            funding.last_draw_digest = Some(digest);
            funding.last_draw_mist = Some(amount);
            funding.last_draw_ms = Some(now_ms);
            funding.draws = funding.draws.saturating_add(1);
        }
    }
    Ok(*funding != before)
}

/// Inventory reason for telemetry: blocked funding, with the queued receipts.
pub fn reason(funding: &Funding, pending_receipts: u32) -> Option<String> {
    funding.blocked.as_ref().map(|blocked| {
        format!("network funding: {blocked}; {pending_receipts} receipt(s) queued until gas is available")
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn config() -> BudgetConfig {
        BudgetConfig { budget_id: "0xb0".into(), low_water_mist: 20, draw_mist: 50 }
    }
    fn budget() -> Budget {
        Budget { version: 1, paused: false, balance: 1_000, registry_id: "0xbe".into() }
    }
    fn account() -> Account {
        Account {
            suspended: false,
            float_cap: 40,
            period_ms: 1_000,
            period_allowance: 100,
            max_draws_per_period: 3,
            min_draw_interval_ms: 10,
            period_start_ms: 0,
            drawn_in_period: 0,
            draws_in_period: 0,
            last_draw_ms: 0,
        }
    }

    #[test]
    fn draws_only_below_low_water_and_within_every_limit() {
        assert_eq!(decide(&config(), 20, &budget(), Some(&account()), 5), Decision::Enough);
        // Asked 50, float cap 40.
        assert_eq!(decide(&config(), 19, &budget(), Some(&account()), 5), Decision::Draw(40));
        // Only 30 left of the allowance.
        let partly = Account { drawn_in_period: 70, draws_in_period: 1, last_draw_ms: 1, ..account() };
        assert_eq!(decide(&config(), 0, &budget(), Some(&partly), 50), Decision::Draw(30));
        // The budget holds less than asked.
        assert_eq!(decide(&config(), 0, &Budget { balance: 7, ..budget() }, Some(&account()), 5), Decision::Draw(7));
    }

    #[test]
    fn explains_every_refusal_the_contract_would_make() {
        let blocked = |budget: Budget, account: Option<Account>, now: u64| match decide(&config(), 0, &budget, account.as_ref(), now) {
            Decision::Blocked(reason) => reason,
            other => panic!("expected a refusal, got {other:?}"),
        };
        assert!(blocked(Budget { paused: true, ..budget() }, Some(account()), 5).contains("paused"));
        assert!(blocked(budget(), None, 5).contains("not enrolled"));
        assert!(blocked(budget(), Some(Account { suspended: true, ..account() }), 5).contains("suspended"));
        assert!(blocked(budget(), Some(Account { drawn_in_period: 100, draws_in_period: 1, last_draw_ms: 1, ..account() }), 50).contains("used up"));
        assert!(blocked(budget(), Some(Account { draws_in_period: 3, last_draw_ms: 1, ..account() }), 50).contains("draw limit"));
        assert!(blocked(budget(), Some(Account { draws_in_period: 1, last_draw_ms: 100, ..account() }), 105).contains("rate limited"));
        assert!(blocked(Budget { balance: 0, ..budget() }, Some(account()), 5).contains("empty"));
    }

    #[test]
    fn a_new_period_restores_the_allowance() {
        let used = Account { drawn_in_period: 100, draws_in_period: 3, last_draw_ms: 900, ..account() };
        assert!(matches!(decide(&config(), 0, &budget(), Some(&used), 999), Decision::Blocked(_)));
        assert_eq!(decide(&config(), 0, &budget(), Some(&used), 1_000), Decision::Draw(40));
    }

    #[test]
    fn parses_the_nodes_json_rendering() {
        let budget = Budget::from_json(&json!({"version": "1", "paused": false, "balance": {"value": "123"}, "registry_id": "0xbe"})).unwrap();
        assert_eq!(budget, Budget { version: 1, paused: false, balance: 123, registry_id: "0xbe".into() });
        assert_eq!(Budget::from_json(&json!({"version": 1, "paused": true, "balance": "9"})).unwrap().balance, 9);
        assert!(Budget::from_json(&json!({"version": "1", "paused": "no", "balance": "9"})).is_err());
        let account = Account::from_json(&json!({
            "owner_cap_id": "0xc1", "suspended": false, "enrolled_at_ms": "1", "total_drawn": "5",
            "limits": {"float_cap": "40", "period_ms": "1000", "period_allowance": "100", "max_draws_per_period": "3", "min_draw_interval_ms": "10"},
            "period_start_ms": "0", "drawn_in_period": "0", "draws_in_period": "0", "last_draw_ms": "0"
        })).unwrap();
        assert_eq!(account, self::account());
        assert!(Account::from_json(&json!({"suspended": false})).is_err());
    }

    #[test]
    fn blocked_since_is_kept_while_the_reason_holds() {
        let mut funding = Funding::default();
        funding.block("empty".into(), 5);
        funding.block("empty".into(), 9);
        assert_eq!(funding.blocked_since_ms, Some(5));
        funding.block("paused".into(), 12);
        assert_eq!(funding.blocked_since_ms, Some(12));
        assert_eq!(reason(&funding, 2).unwrap(), "network funding: paused; 2 receipt(s) queued until gas is available");
        funding.clear();
        assert_eq!(reason(&funding, 2), None);
    }

    #[test]
    fn config_needs_budget_origins_and_a_positive_draw() {
        let manifest: crate::config::DeploymentManifest =
            serde_json::from_str(&std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../manifests/fixture.localnet.json")).unwrap()).unwrap();
        assert!(config().validate(&manifest).unwrap_err().to_string().contains("fleet budget type origins"));
        let mut with_budget = manifest.clone();
        for name in crate::protocol::BUDGET_TYPES {
            with_budget.type_origins.insert((*name).to_owned(), with_budget.call_package_id.clone());
        }
        config().validate(&with_budget).unwrap();
        assert!(BudgetConfig { draw_mist: 0, ..config() }.validate(&with_budget).is_err());
        assert!(BudgetConfig { budget_id: "budget".into(), ..config() }.validate(&with_budget).is_err());
    }
}
