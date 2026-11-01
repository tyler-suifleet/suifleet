//! Admission policy. All times are UTC Unix milliseconds, windows are [start,end).
//! Controls gate the entire dependency plan before its first disruptive install.
//! An admitted plan finishes normally; cancelling cannot safely undo an updater.
//! SF-174 recurring weekly windows are evaluated here in their IANA zone with
//! the tz database pinned into this binary, never the host zoneinfo or `TZ`.
use jiff::{civil, tz::{AmbiguousOffset, TimeZone, TimeZoneDatabase}, Timestamp};
use serde::{Deserialize, Serialize};
use std::sync::OnceLock;

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
fn minutes<'de, D: serde::Deserializer<'de>>(d: D) -> Result<u16, D::Error> {
    u16::try_from(number(d)?).map_err(serde::de::Error::custom)
}

/// One weekly slot: opens at a local wall-clock minute on each selected day
/// and lasts `duration_minutes` of elapsed time (it may cross midnight).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WeeklySlot {
    #[serde(deserialize_with = "byte")]
    pub days: u8, // bit 0 Monday ... bit 6 Sunday
    #[serde(deserialize_with = "minutes")]
    pub start_minute: u16,
    #[serde(deserialize_with = "minutes")]
    pub duration_minutes: u16,
}
/// `deployment_manager::RecurringWindow`. It ANDs with every Control rule.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Recurring {
    #[serde(deserialize_with = "number")]
    pub revision: u64,
    pub timezone: String,
    pub slots: Vec<WeeklySlot>,
}
/// Reserved for a later per-device timezone; not supported yet.
pub const DEVICE_LOCAL: &str = "device-local";
/// IANA release of the bundled tz database, recorded with deferrals.
pub fn tzdb_version() -> &'static str { jiff_tzdb::VERSION.unwrap_or("unknown") }

fn zone(name: &str) -> Result<TimeZone, &'static str> {
    if name == DEVICE_LOCAL { return Err("device-local timezone not supported"); }
    static DB: OnceLock<TimeZoneDatabase> = OnceLock::new();
    let tz = DB.get_or_init(TimeZoneDatabase::bundled).get(name).map_err(|_| "unknown timezone")?;
    // Lookup ignores case; accept only the exact name the editor showed.
    if tz.is_unknown() || tz.iana_name() != Some(name) { return Err("unknown timezone"); }
    Ok(tz)
}
/// The UTC instant a local wall-clock time opens. In a DST gap that is the
/// first valid instant after the gap; in an overlap, the earlier occurrence.
fn opening(tz: &TimeZone, date: civil::Date, minute: u16) -> Option<Timestamp> {
    let local = date.at((minute / 60) as i8, (minute % 60) as i8, 0, 0);
    match tz.to_ambiguous_timestamp(local).offset() {
        AmbiguousOffset::Unambiguous { offset } => offset.to_timestamp(local).ok(),
        AmbiguousOffset::Fold { before, after } =>
            Some(before.to_timestamp(local).ok()?.min(after.to_timestamp(local).ok()?)),
        AmbiguousOffset::Gap { before, after } => {
            let earliest = before.to_timestamp(local).ok()?.min(after.to_timestamp(local).ok()?);
            tz.following(earliest).next().map(|transition| transition.timestamp())
        }
    }
}
impl Recurring {
    fn check(&self) -> Result<TimeZone, &'static str> {
        let valid = (1..=64).contains(&self.timezone.len()) && (1..=8).contains(&self.slots.len())
            && self.slots.iter().all(|s| (1..128).contains(&s.days) && s.start_minute < 1440
                && (1..=1440).contains(&s.duration_minutes));
        if !valid { return Err("invalid recurring maintenance window"); }
        zone(&self.timezone)
    }
    /// The occurrence containing `now`, else the next one, as UTC `[open, close)`
    /// milliseconds. Overlapping or touching slots merge. An error is the
    /// fail-closed deferral reason.
    pub fn occurrence(&self, now: u64) -> Result<(u64, u64), &'static str> {
        let tz = self.check()?;
        let invalid = "invalid recurring maintenance window";
        let instant = Timestamp::from_millisecond(i64::try_from(now).map_err(|_| invalid)?).map_err(|_| invalid)?;
        // A slot lasts at most a day, so one that opened two local days ago
        // has closed; every nonempty day mask opens within the next week.
        let mut date = instant.to_zoned(tz.clone()).date().yesterday().and_then(|d| d.yesterday()).map_err(|_| invalid)?;
        let mut spans = Vec::new();
        for _ in 0..10 {
            let bit = 1u8 << date.weekday().to_monday_zero_offset();
            for slot in self.slots.iter().filter(|s| s.days & bit != 0) {
                let open = opening(&tz, date, slot.start_minute).ok_or(invalid)?;
                let open = u64::try_from(open.as_millisecond()).map_err(|_| invalid)?;
                spans.push((open, open + u64::from(slot.duration_minutes) * 60_000));
            }
            date = date.tomorrow().map_err(|_| invalid)?;
        }
        spans.sort_unstable();
        let mut merged: Vec<(u64, u64)> = Vec::new();
        for (open, close) in spans {
            match merged.last_mut() {
                Some(last) if open <= last.1 => last.1 = last.1.max(close),
                _ => merged.push((open, close)),
            }
        }
        merged.into_iter().find(|&(_, close)| now < close).ok_or(invalid)
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Deferred {
    pub control: Control,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recurring: Option<Recurring>,
    pub notified_ms: u64,
    pub reason: String,
    pub terminal: bool,
    /// UTC opening of the next recurring window, while outside one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub next_window_ms: Option<u64>,
    /// Bundled tz database that evaluated the recurring window.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tzdb: Option<String>,
}
impl Control {
    pub fn reason(&self, now: u64, notified: u64, recurring: Option<&Recurring>) -> anyhow::Result<Option<&'static str>> {
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
        } else if let Some(reason) = recurring.and_then(|r| match r.occurrence(now) {
            Err(reason) => Some(reason),
            Ok((open, _)) => (now < open).then_some("outside recurring maintenance window"),
        }) { Some(reason) }
        else if now < notified.saturating_add(self.notice_ms) { Some("advance notice") }
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
        assert_eq!(c.reason(9, 0, None).unwrap(), Some("not yet scheduled"));
        assert_eq!(c.reason(19, 0, None).unwrap(), Some("outside maintenance window"));
        assert_eq!(c.reason(20, 18, None).unwrap(), Some("advance notice"));
        assert_eq!(c.reason(23, 18, None).unwrap(), None);
        assert_eq!(c.reason(79, 18, None).unwrap(), None);
        assert_eq!(c.reason(80, 18, None).unwrap(), Some("outside maintenance window"));
        assert_eq!(c.reason(100, 18, None).unwrap(), Some("expired"));
        assert_eq!(Control { state: 1, ..c.clone() }.reason(100, 0, None).unwrap(), Some("expired"));
        assert_eq!(Control { state: 2, ..c }.reason(100, 0, None).unwrap(), Some("cancelled"));
    }
    #[test]
    fn malformed_or_future_controls_fail_closed() {
        for c in [Control { state: 3, ..Control::default() },
            Control { window_start_ms: 5, ..Control::default() },
            Control { expires_ms: 5, not_before_ms: 5, ..Control::default() },
            Control { notice_ms: 604_800_001, ..Control::default() }] {
            assert!(c.reason(0, 0, None).is_err());
        }
        assert!(serde_json::from_str::<Control>(r#"{"state":0}"#).is_err());
    }

    const H: u64 = 3_600_000;
    const M: u64 = 60_000;
    fn weekly(timezone: &str, slots: &[(u8, u16, u16)]) -> Recurring {
        Recurring { revision: 1, timezone: timezone.into(), slots: slots.iter()
            .map(|&(days, start_minute, duration_minutes)| WeeklySlot { days, start_minute, duration_minutes }).collect() }
    }
    // Instants below were computed independently with Python zoneinfo.
    #[test]
    fn sunday_window_in_site_zone_and_exclusive_close() {
        let r = weekly("Europe/Berlin", &[(64, 120, 120)]); // Sunday 02:00 for two hours
        let saturday_noon = 1_793_444_400_000; // 2026-10-31 12:00 CET
        let open = 1_793_494_800_000; // 2026-11-01 02:00 CET = 01:00Z
        assert_eq!(r.occurrence(saturday_noon), Ok((open, open + 2 * H)));
        assert_eq!(r.occurrence(open), Ok((open, open + 2 * H)));
        assert_eq!(r.occurrence(open + 2 * H - 1), Ok((open, open + 2 * H)));
        assert_eq!(r.occurrence(open + 2 * H), Ok((open + 168 * H, open + 170 * H)));
        let c = Control::default();
        assert_eq!(c.reason(saturday_noon, 0, Some(&r)).unwrap(), Some("outside recurring maintenance window"));
        assert_eq!(c.reason(open, 0, Some(&r)).unwrap(), None);
        assert_eq!(c.reason(open + 2 * H, 0, Some(&r)).unwrap(), Some("outside recurring maintenance window"));
        assert_eq!(c.reason(saturday_noon, 0, None).unwrap(), None);
    }
    #[test]
    fn dst_gap_opens_at_first_valid_instant() {
        // 2026-03-29 02:30 does not exist in Berlin; 01:00Z is 03:00 CEST.
        let r = weekly("Europe/Berlin", &[(64, 150, 60)]);
        let transition = 1_774_746_000_000;
        assert_eq!(r.occurrence(transition - H), Ok((transition, transition + H)));
        // 2026-03-08 02:15 does not exist in New York; 07:00Z is 03:00 EDT.
        let r = weekly("America/New_York", &[(64, 135, 30)]);
        let transition = 1_772_953_200_000;
        assert_eq!(r.occurrence(transition - H), Ok((transition, transition + 30 * M)));
    }
    #[test]
    fn dst_overlap_opens_once_at_earlier_occurrence() {
        // 2026-10-25 02:30 occurs at 00:30Z (CEST) and 01:30Z (CET).
        let r = weekly("Europe/Berlin", &[(64, 150, 60)]);
        let early = 1_792_888_200_000;
        let late = 1_792_891_800_000;
        assert_eq!(r.occurrence(early - 1), Ok((early, early + H)));
        // The second 02:30 is the earlier window's close, not a new opening.
        assert_eq!(late, early + H);
        let next_week = 1_793_496_600_000; // 2026-11-01 02:30 CET
        assert_eq!(r.occurrence(late + 15 * M), Ok((next_week, next_week + H)));
        // 2026-11-01 01:30 occurs at 05:30Z (EDT) and 06:30Z (EST).
        let r = weekly("America/New_York", &[(64, 90, 30)]);
        let early = 1_793_511_000_000;
        let late = 1_793_514_600_000;
        assert_eq!(r.occurrence(early), Ok((early, early + 30 * M)));
        assert_eq!(r.occurrence(late).unwrap().0, early + 7 * 24 * H + H); // next Sunday, EST
    }
    #[test]
    fn midnight_crossing_and_merged_slots_without_dst() {
        // Sunday 23:00 for three hours in Kolkata (+05:30) ends Monday 02:00.
        let r = weekly("Asia/Kolkata", &[(64, 1380, 180)]);
        let open = 1_793_554_200_000;
        let monday_one = 1_793_561_400_000;
        assert_eq!(r.occurrence(monday_one), Ok((open, 1_793_565_000_000)));
        assert_eq!(Control::default().reason(monday_one, 0, Some(&r)).unwrap(), None);
        // Touching slots coalesce: Sunday 23:00-24:00 and Monday 00:00-01:00.
        let r = weekly("Asia/Kolkata", &[(64, 1380, 60), (1, 0, 60)]);
        assert_eq!(r.occurrence(open + 90 * M), Ok((open, open + 2 * H)));
        // Every day, all day: always open.
        let r = weekly("UTC", &[(127, 0, 1440)]);
        assert!(r.occurrence(open).is_ok_and(|(o, c)| o <= open && open < c));
    }
    #[test]
    fn recurring_window_ands_with_other_controls() {
        let r = weekly("Europe/Berlin", &[(64, 120, 120)]);
        let open = 1_793_494_800_000;
        let c = Control { not_before_ms: open + H, notice_ms: 10 * M, ..Control::default() };
        assert_eq!(c.reason(open, 0, Some(&r)).unwrap(), Some("not yet scheduled"));
        assert_eq!(c.reason(open + H, open + H - 5 * M, Some(&r)).unwrap(), Some("advance notice"));
        assert_eq!(c.reason(open + H, 0, Some(&r)).unwrap(), None);
        assert_eq!(Control { state: 1, ..c.clone() }.reason(open + H, 0, Some(&r)).unwrap(), Some("paused"));
        let window = Control { window_start_ms: open, window_end_ms: open + 30 * M, ..Control::default() };
        assert_eq!(window.reason(open + 40 * M, 0, Some(&r)).unwrap(), Some("outside maintenance window"));
        let expired = Control { expires_ms: open + 10 * M, ..Control::default() };
        assert_eq!(expired.reason(open + 10 * M, 0, Some(&r)).unwrap(), Some("expired"));
    }
    #[test]
    fn unknown_reserved_or_malformed_recurring_windows_fail_closed() {
        let now = 1_793_444_400_000;
        let c = Control::default();
        for (r, reason) in [
            (weekly("Mars/Olympus_Mons", &[(64, 120, 120)]), "unknown timezone"),
            (weekly("europe/berlin", &[(64, 120, 120)]), "unknown timezone"),
            (weekly("Etc/Unknown", &[(64, 120, 120)]), "unknown timezone"),
            (weekly(DEVICE_LOCAL, &[(64, 120, 120)]), "device-local timezone not supported"),
            (weekly("UTC", &[]), "invalid recurring maintenance window"),
            (weekly("UTC", &[(0, 120, 120)]), "invalid recurring maintenance window"),
            (weekly("UTC", &[(128, 120, 120)]), "invalid recurring maintenance window"),
            (weekly("UTC", &[(1, 1440, 1)]), "invalid recurring maintenance window"),
            (weekly("UTC", &[(1, 0, 0)]), "invalid recurring maintenance window"),
            (weekly("UTC", &[(1, 0, 1441)]), "invalid recurring maintenance window"),
            (weekly("UTC", &[(1, 0, 1); 9]), "invalid recurring maintenance window"),
        ] {
            assert_eq!(c.reason(now, 0, Some(&r)).unwrap(), Some(reason), "{r:?}");
        }
        // Lifecycle states still take precedence over a broken window.
        let broken = weekly("Mars/Olympus_Mons", &[(64, 120, 120)]);
        assert_eq!(Control { state: 2, ..Control::default() }.reason(now, 0, Some(&broken)).unwrap(), Some("cancelled"));
        assert!(tzdb_version().starts_with("20"));
    }
    #[test]
    fn chain_json_layout_parses_strictly() {
        let r: Recurring = serde_json::from_str(r#"{"revision":"3","timezone":"Europe/Berlin",
            "slots":[{"days":64,"start_minute":120,"duration_minutes":"120"}]}"#).unwrap();
        assert_eq!(r, Recurring { revision: 3, ..weekly("Europe/Berlin", &[(64, 120, 120)]) });
        assert!(serde_json::from_str::<Recurring>(r#"{"revision":1,"timezone":"UTC","slots":[{"days":1,"start_minute":70000,"duration_minutes":1}]}"#).is_err());
        assert!(serde_json::from_str::<Recurring>(r#"{"revision":1,"timezone":"UTC","slots":[],"extra":1}"#).is_err());
        // Journals written before SF-174 still load.
        let old: Deferred = serde_json::from_str(r#"{"control":{"revision":0,"state":0,"not_before_ms":0,"expires_ms":0,
            "window_start_ms":0,"window_end_ms":0,"notice_ms":0},"notified_ms":0,"reason":"paused","terminal":false}"#).unwrap();
        assert!(old.recurring.is_none() && old.next_window_ms.is_none());
        assert!(!serde_json::to_string(&old).unwrap().contains("recurring"));
    }
}

