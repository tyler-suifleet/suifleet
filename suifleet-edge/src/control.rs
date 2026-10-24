//! Admission policy. All times are UTC Unix milliseconds, windows are [start,end).
//! Controls gate the entire dependency plan before its first disruptive install.
//! An admitted plan finishes normally; cancelling cannot safely undo an updater.
use serde::{Deserialize, Serialize};

#[derive(Debug, Default, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Control {
    #[serde(deserialize_with = "number")]
    pub revision: u64,
    #[serde(deserialize_with = "byte")]
    pub state: u8,
    #[serde(deserialize_with = "number")]
    pub not_before_ms: u64,
    #[serde(deserialize_with = "number")]
    pub expires_ms: u64,
    #[serde(deserialize_with = "number")]
    pub window_start_ms: u64,
    #[serde(deserialize_with = "number")]
    pub window_end_ms: u64,
    #[serde(deserialize_with = "number")]
    pub notice_ms: u64,
}
fn number<'de, D: serde::Deserializer<'de>>(d: D) -> Result<u64, D::Error> {
    let v = serde_json::Value::deserialize(d)?;
    v.as_u64().or_else(|| v.as_str()?.parse().ok()).or_else(|| {
        let n = v.as_f64()?;
        (n.is_finite() && n >= 0.0 && n <= 9_007_199_254_740_991.0 && n.fract() == 0.0)
            .then_some(n as u64)
    })
        .ok_or_else(|| serde::de::Error::custom("invalid u64 control time"))
}
fn byte<'de, D: serde::Deserializer<'de>>(d: D) -> Result<u8, D::Error> {
    u8::try_from(number(d)?).map_err(serde::de::Error::custom)
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Deferred {
    pub control: Control,
    pub notified_ms: u64,
    pub reason: String,
    pub terminal: bool,
}
impl Control {
    pub fn reason(&self, now: u64, notified: u64) -> anyhow::Result<Option<&'static str>> {
        anyhow::ensure!(self.state <= 2, "unsupported deployment control state");
        anyhow::ensure!(self.expires_ms == 0 || self.expires_ms > self.not_before_ms,
            "invalid deployment expiry");
        anyhow::ensure!((self.window_start_ms == 0 && self.window_end_ms == 0) ||
            self.window_end_ms > self.window_start_ms, "invalid maintenance window");
        anyhow::ensure!(self.notice_ms <= 604_800_000, "invalid notice duration");
        Ok(if self.state == 2 { Some("cancelled") }
        else if self.expires_ms != 0 && now >= self.expires_ms { Some("expired") }
        else if self.state == 1 { Some("paused") }
        else if now < self.not_before_ms { Some("not yet scheduled") }
        else if self.window_end_ms != 0 && (now < self.window_start_ms || now >= self.window_end_ms) {
            Some("outside maintenance window")
        } else if now < notified.saturating_add(self.notice_ms) { Some("advance notice") }
        else { None })
    }
}

#[derive(Debug)]
pub struct NotAdmitted;
impl std::fmt::Display for NotAdmitted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { f.write_str("deployment deferred before apply") }
}
impl std::error::Error for NotAdmitted {}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn deadlines_windows_and_notice_use_exclusive_end() {
        let c = Control { not_before_ms: 10, expires_ms: 100, window_start_ms: 20,
            window_end_ms: 80, notice_ms: 5, ..Control::default() };
        assert_eq!(c.reason(9, 0).unwrap(), Some("not yet scheduled"));
        assert_eq!(c.reason(19, 0).unwrap(), Some("outside maintenance window"));
        assert_eq!(c.reason(20, 18).unwrap(), Some("advance notice"));
        assert_eq!(c.reason(23, 18).unwrap(), None);
        assert_eq!(c.reason(79, 18).unwrap(), None);
        assert_eq!(c.reason(80, 18).unwrap(), Some("outside maintenance window"));
        assert_eq!(c.reason(100, 18).unwrap(), Some("expired"));
        assert_eq!(Control { state: 1, ..c.clone() }.reason(100, 0).unwrap(), Some("expired"));
        assert_eq!(Control { state: 2, ..c }.reason(100, 0).unwrap(), Some("cancelled"));
    }
    #[test]
    fn malformed_or_future_controls_fail_closed() {
        for c in [Control { state: 3, ..Control::default() },
            Control { window_start_ms: 5, ..Control::default() },
            Control { expires_ms: 5, not_before_ms: 5, ..Control::default() },
            Control { notice_ms: 604_800_001, ..Control::default() }] {
            assert!(c.reason(0, 0).is_err());
        }
        assert!(serde_json::from_str::<Control>(r#"{"state":0}"#).is_err());
    }
}
