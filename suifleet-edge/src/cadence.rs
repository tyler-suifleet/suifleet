//! Local wakeups never authorize an apply: every wake performs fresh admission reads.
use crate::{
    config::RuntimeConfig,
    control::{Control, Deferred, Recurring},
};
use rand::Rng;
use serde::{Deserialize, Serialize};
use std::time::Duration;

/// Calendar bookkeeping, never admission authority. Overlapping observed
/// windows coalesce so completed jobs do not grow an unbounded tracking map.
#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
pub struct Window {
    pub start_ms: u64,
    pub end_ms: u64,
}
impl Window {
    fn control(control: &Control) -> Option<Self> {
        let end_ms = if control.expires_ms == 0 {
            control.window_end_ms
        } else {
            control.window_end_ms.min(control.expires_ms)
        };
        (control.state != 2 && end_ms > control.window_start_ms).then_some(Self {
            start_ms: control.window_start_ms,
            end_ms,
        })
    }
    /// The recurring occurrence containing or following `now`, bounded by
    /// expiry. Unknown zones yield none: admission already fails closed.
    fn recurring(control: &Control, recurring: Option<&Recurring>, now: u64) -> Option<Self> {
        let (start_ms, end_ms) = recurring?.occurrence(now).ok()?;
        let end_ms = if control.expires_ms == 0 { end_ms } else { end_ms.min(control.expires_ms) };
        (control.state != 2 && end_ms > start_ms).then_some(Self { start_ms, end_ms })
    }
    fn windows(control: &Control, recurring: Option<&Recurring>, now: u64) -> impl Iterator<Item = Self> {
        [Self::control(control), Self::recurring(control, recurring, now)].into_iter().flatten()
    }
    pub fn remember(current: &mut Option<Self>, control: &Control, recurring: Option<&Recurring>, now: u64) {
        for window in Self::windows(control, recurring, now).filter(|w| w.start_ms <= now && now < w.end_ms) {
            *current = Some(match *current {
                Some(old) if old.end_ms > now => Self {
                    start_ms: old.start_ms.min(window.start_ms),
                    end_ms: old.end_ms.max(window.end_ms),
                },
                _ => window,
            });
        }
    }
    fn delay(self, now: u64, fast_ms: u64) -> Option<u64> {
        if self.start_ms >= self.end_ms || now >= self.end_ms {
            return None;
        }
        Some(if now < self.start_ms {
            self.start_ms - now
        } else {
            fast_ms.min(self.end_ms - now)
        })
    }
}

#[derive(Default)]
pub struct Cadence {
    failures: u32,
    poll_started_ms: u64,
}
impl Cadence {
    pub fn begin_poll(&mut self, now: u64) {
        self.poll_started_ms = now;
    }

    /// A pending deployment signal is known rollout work, not a new normal
    /// discovery. Preserve exponential backoff when discovery itself failed.
    pub fn post_apply_delay(&self, config: &RuntimeConfig, normal: Duration, pending: bool) -> Duration {
        if pending && self.failures == 0 {
            normal.min(Duration::from_secs(config.rollout_poll_seconds.min(config.poll_seconds)))
        } else { normal }
    }

    pub fn record(&mut self, success: bool) {
        self.failures = if success {
            0
        } else {
            self.failures.saturating_add(1)
        };
    }
    pub fn delay<'a>(
        &self,
        config: &RuntimeConfig,
        controls: impl Iterator<Item = &'a Deferred>,
        now: u64,
        window: Option<Window>,
    ) -> Duration {
        self.delay_with(config, controls, now, window, &mut rand::thread_rng())
    }
    fn delay_with<'a>(
        &self,
        config: &RuntimeConfig,
        controls: impl Iterator<Item = &'a Deferred>,
        now: u64,
        window: Option<Window>,
        rng: &mut impl Rng,
    ) -> Duration {
        if self.failures > 0 {
            // Equal jitter, bounded to one hour. Millisecond entropy avoids fleets
            // choosing from only a handful of second-sized retry buckets.
            let cap = (1000u64 << self.failures.min(12))
                .min(config.poll_seconds.saturating_mul(2000).min(3_600_000));
            return Duration::from_millis(rng.gen_range(cap / 2..=cap));
        }
        let mut delay = config.poll_seconds.saturating_mul(1000);
        let fast_ms = config.rollout_poll_seconds.min(config.poll_seconds) * 1000;
        if let Some(next) = window.and_then(|w| w.delay(now, fast_ms)) {
            delay = delay.min(next);
        }
        for d in controls.filter(|d| !d.terminal) {
            let c = &d.control;
            let recurring = d.recurring.as_ref();
            for next in Window::windows(c, recurring, now).filter_map(|w| w.delay(now, fast_ms)) {
                delay = delay.min(next);
            }
            if c.state != 0 || (c.expires_ms != 0 && now >= c.expires_ms) {
                continue;
            }
            let mut start = c
                .not_before_ms
                .max(c.window_start_ms)
                .max(d.notified_ms.saturating_add(c.notice_ms));
            // The recurring occurrence containing or following that instant.
            if let Some(Ok((open, _))) = recurring.map(|r| r.occurrence(start.max(now))) {
                start = start.max(open);
            }
            // Arm the admission time, not each constituent constraint: an
            // earlier wake must not consume a later, shorter maintenance window.
            if start > now
                && (c.window_end_ms == 0 || start < c.window_end_ms)
                && (c.expires_ms == 0 || start < c.expires_ms)
            {
                delay = delay.min(start - now);
            }
            // Reporting after admission reads can cross the start of a short
            // window. Revisit a now-eligible temporal deferral immediately;
            // sleeping a full fast interval could consume the entire window.
            // Only do this if the boundary crossed during this poll, so an
            // obsolete deferral cannot cause a busy loop on later polls.
            // This is only a wakeup hint; the next poll rechecks chain authority.
            if matches!(d.reason.as_str(), "not yet scheduled" | "outside maintenance window"
                    | "outside recurring maintenance window" | "advance notice")
                && start >= self.poll_started_ms
                && matches!(c.reason(now, d.notified_ms, recurring), Ok(None))
            {
                delay = 1;
            }
            if d.reason == "waiting for rollout eligibility" || d.reason == "ready for admission" {
                delay = delay.min(fast_ms);
            }
        }
        Duration::from_millis(delay.max(1))
    }
}
/// Idle version checks do not scan events at every short test/customer poll.
/// An observed active window, including one retained after completion, does
/// require discovery at the configured fast cadence for subsequent work.
pub fn discovery_bound<'a>(
    config: &RuntimeConfig,
    controls: impl Iterator<Item = &'a Deferred>,
    now: u64,
    window: Option<Window>,
) -> u64 {
    let active = |w: Window| w.start_ms <= now && now < w.end_ms;
    let fast = window.is_some_and(active) || controls.filter(|d| !d.terminal).any(|d| {
        Window::windows(&d.control, d.recurring.as_ref(), now).any(active)
            || (d.control.state == 0
                && (d.control.expires_ms == 0 || now < d.control.expires_ms)
                && matches!(d.reason.as_str(), "waiting for rollout eligibility" | "ready for admission"))
    });
    if fast {
        config.event_discovery_seconds.min(config.rollout_poll_seconds).min(config.poll_seconds)
    } else {
        config.event_discovery_seconds
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::Control;
    use rand::{rngs::StdRng, SeedableRng};
    fn deferred(control: Control) -> Deferred {
        Deferred {
            control,
            recurring: None,
            notified_ms: 0,
            reason: "not yet scheduled".into(),
            terminal: false,
            next_window_ms: None,
            tzdb: None,
        }
    }
    #[test]
    fn pending_health_wakes_fast_without_overriding_rpc_failure_backoff() {
        let mut cfg = RuntimeConfig::default();
        let c = Cadence::default();
        assert_eq!(c.post_apply_delay(&cfg, Duration::from_secs(1800), false).as_secs(), 1800);
        assert_eq!(c.post_apply_delay(&cfg, Duration::from_secs(1800), true).as_secs(), 60);
        cfg.poll_seconds = 1;
        assert_eq!(c.post_apply_delay(&cfg, Duration::from_secs(1), true).as_secs(), 1);
        let failed = Cadence { failures: 10, ..Default::default() };
        assert_eq!(failed.post_apply_delay(&cfg, Duration::from_secs(512), true).as_secs(), 512);
    }

    #[test]
    fn legacy_unreported_and_failed_health_preserve_normal_cadence() {
        let cfg = RuntimeConfig::default();
        let c = Cadence::default();
        let mut o: crate::state::Outcome = serde_json::from_str(
            r#"{"status":1,"reported":false,"error":null}"#).unwrap();
        let delay = |o: &crate::state::Outcome| c.post_apply_delay(&cfg,
            c.delay(&cfg, [].iter(), 0, None), o.fast_rollout_health_pending());
        assert_eq!(delay(&o).as_secs(), 1800);
        o.reported = true;
        assert_eq!(delay(&o).as_secs(), 60);
        o.rollout_health_slow_retry = true;
        let restored = serde_json::from_slice(&serde_json::to_vec(&o).unwrap()).unwrap();
        assert_eq!(delay(&restored).as_secs(), 1800);
        assert!(restored.rollout_health_pending); // Still recoverable on a later normal poll.
    }

    #[test]
    fn idle_batch_bound_and_retained_fast_discovery_are_independent() {
        let mut cfg = RuntimeConfig { poll_seconds: 1, ..Default::default() };
        assert_eq!(discovery_bound(&cfg, [].iter(), 5, None), 60);
        cfg.poll_seconds = 1800; cfg.rollout_poll_seconds = 2;
        let window = Some(Window { start_ms: 10, end_ms: 20 });
        assert_eq!(discovery_bound(&cfg, [].iter(), 9, window), 60);
        assert_eq!(discovery_bound(&cfg, [].iter(), 10, window), 2);
        assert_eq!(discovery_bound(&cfg, [].iter(), 19, window), 2); // Completed job's retained window.
        assert_eq!(discovery_bound(&cfg, [].iter(), 20, window), 60);
        let mut pending = deferred(Control::default()); pending.reason = "waiting for rollout eligibility".into();
        assert_eq!(discovery_bound(&cfg, [&pending].into_iter(), 5, None), 2);
        pending.terminal = true;
        assert_eq!(discovery_bound(&cfg, [&pending].into_iter(), 5, None), 60);
    }
    #[test]
    fn defaults_fast_windows_and_short_local_deadlines() {
        let cfg = RuntimeConfig::default();
        let c = Cadence::default();
        assert_eq!(c.delay(&cfg, [].iter(), 0, None).as_secs(), 1800);
        let d = deferred(Control {
            not_before_ms: 5000,
            window_start_ms: 6000,
            window_end_ms: 7000,
            ..Default::default()
        });
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 0, None).as_millis(), 6000);
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 6000, None).as_millis(), 1);
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 7000, None).as_secs(), 1800);
        let d = deferred(Control {
            notice_ms: 2500,
            ..Default::default()
        });
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 0, None).as_millis(), 2500);
    }
    #[test]
    fn recurring_windows_arm_next_opening_and_fast_cadence_while_open() {
        use crate::control::{Recurring, WeeklySlot};
        let cfg = RuntimeConfig::default();
        let c = Cadence::default();
        let saturday_noon = 1_793_444_400_000; // 2026-10-31 12:00 Europe/Berlin
        let open = 1_793_494_800_000; // Sunday 02:00 CET
        let mut d = deferred(Control::default());
        d.reason = "outside recurring maintenance window".into();
        d.recurring = Some(Recurring { revision: 1, timezone: "Europe/Berlin".into(),
            slots: vec![WeeklySlot { days: 64, start_minute: 120, duration_minutes: 120 }] });
        // Closed: wake for the opening (or the routine poll, if sooner).
        assert_eq!(c.delay(&cfg, [&d].into_iter(), open - 60_000, None).as_millis(), 60_000);
        assert_eq!(c.delay(&cfg, [&d].into_iter(), saturday_noon, None).as_secs(), 1800);
        // Crossing the opening during a poll rechecks at once.
        assert_eq!(c.delay(&cfg, [&d].into_iter(), open + 1, None).as_millis(), 1);
        // Open: fast discovery until it closes, then the routine bound again.
        let fast = RuntimeConfig { rollout_poll_seconds: 2, ..Default::default() };
        assert_eq!(discovery_bound(&fast, [&d].into_iter(), open + 1, None), 2);
        assert_eq!(discovery_bound(&fast, [&d].into_iter(), open + 7_200_000, None), 60);
        // Notice ending after this week's close arms next week's opening.
        let mut late = d.clone();
        late.control.notice_ms = 604_800_000;
        late.notified_ms = open;
        let next_open = open + 7 * 86_400_000;
        assert_eq!(c.delay(&cfg, [&late].into_iter(), next_open - 1000, None).as_millis(), 1000);
        // An expiry before the opening never arms it; unknown zones fail closed.
        let mut expiring = d.clone();
        expiring.control.expires_ms = open - 1;
        assert_eq!(c.delay(&cfg, [&expiring].into_iter(), open - 60_000, None).as_secs(), 1800);
        let mut unknown = d.clone();
        unknown.recurring.as_mut().unwrap().timezone = "Mars/Olympus_Mons".into();
        assert_eq!(c.delay(&cfg, [&unknown].into_iter(), open - 60_000, None).as_secs(), 1800);
        // Remembered while open, so post-completion polling stays fast.
        let mut window = None;
        Window::remember(&mut window, &Control::default(), d.recurring.as_ref(), saturday_noon);
        assert!(window.is_none());
        Window::remember(&mut window, &Control::default(), d.recurring.as_ref(), open + 1);
        assert_eq!(window, Some(Window { start_ms: open, end_ms: open + 7_200_000 }));
    }
    #[test]
    fn report_retry_crossing_admission_deadline_rechecks_without_consuming_window() {
        let cfg = RuntimeConfig::default();
        let cadence = Cadence::default();
        for reason in ["not yet scheduled", "outside maintenance window", "advance notice"] {
            let mut d = deferred(Control {
                not_before_ms: 1000, window_start_ms: 1000, window_end_ms: 2000,
                notice_ms: 1000, ..Default::default()
            });
            d.reason = reason.into();
            assert_eq!(cadence.delay(&cfg, [&d].into_iter(), 999, None).as_millis(), 1);
            assert_eq!(cadence.delay(&cfg, [&d].into_iter(), 1001, None).as_millis(), 1);
            assert_eq!(cadence.delay(&cfg, [&d].into_iter(), 2000, None).as_secs(), 1800);
            d.control.state = 1;
            assert_ne!(cadence.delay(&cfg, [&d].into_iter(), 1001, None).as_millis(), 1);
            d.control.state = 0;
            let mut already_checked = Cadence::default();
            already_checked.begin_poll(1001);
            assert_ne!(already_checked.delay(&cfg, [&d].into_iter(), 1002, None).as_millis(), 1,
                "an obsolete temporal deferral must not create a busy loop");
            let failed = Cadence { failures: 3, ..Default::default() };
            assert!(failed.delay(&cfg, [&d].into_iter(), 1001, None).as_millis() >= 4000);
        }
    }

    #[test]
    fn completed_windows_survive_journal_restart_and_expire() {
        let cfg = RuntimeConfig::default();
        let c = Cadence::default();
        let mut state = crate::state::State::default();
        let old = serde_json::to_value(&state).unwrap();
        assert!(old.get("check_window").is_none());
        assert!(serde_json::from_value::<crate::state::State>(old)
            .unwrap()
            .check_window
            .is_none());
        Window::remember(
            &mut state.check_window,
            &Control {
                window_start_ms: 5000,
                window_end_ms: 600000,
                ..Default::default()
            }, None,
            6000,
        );
        Window::remember(
            &mut state.check_window,
            &Control {
                window_start_ms: 5500,
                window_end_ms: 900000,
                ..Default::default()
            }, None,
            6500,
        );
        // No deferrals remain after completion. Only the bounded window persists.
        let restored: crate::state::State =
            serde_json::from_slice(&serde_json::to_vec(&state).unwrap()).unwrap();
        assert_eq!(
            c.delay(&cfg, [].iter(), 6500, restored.check_window)
                .as_secs(),
            60
        );
        assert_eq!(
            c.delay(&cfg, [].iter(), 899500, restored.check_window)
                .as_millis(),
            500
        );
        assert_eq!(
            c.delay(&cfg, [].iter(), 900000, restored.check_window)
                .as_secs(),
            1800
        );
        assert_eq!(
            c.delay(&cfg, [].iter(), 0, restored.check_window)
                .as_millis(),
            5000
        );
    }
    #[test]
    fn fleet_failures_spread_and_recovery_resets() {
        let cfg = RuntimeConfig::default();
        let mut c = Cadence::default();
        let mut rng = StdRng::seed_from_u64(123);
        c.record(false);
        let delays: std::collections::HashSet<_> = (0..1000)
            .map(|_| c.delay_with(&cfg, [].iter(), 0, None, &mut rng).as_millis())
            .collect();
        assert!(delays.len() > 500);
        for _ in 0..100 {
            c.record(false);
        }
        let delay = c.delay(&cfg, [].iter(), 0, None).as_secs();
        assert!((1800..=3600).contains(&delay));
        c.record(true);
        assert_eq!(c.delay(&cfg, [].iter(), 0, None).as_secs(), 1800);
    }
}
