//! Local wakeups never authorize an apply: every wake performs fresh admission reads.
use crate::{
    config::RuntimeConfig,
    control::{Control, Deferred},
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
    pub fn remember(current: &mut Option<Self>, control: &Control, now: u64) {
        let Some(window) = Self::control(control).filter(|w| w.start_ms <= now && now < w.end_ms)
        else {
            return;
        };
        *current = Some(match *current {
            Some(old) if old.end_ms > now => Self {
                start_ms: old.start_ms.min(window.start_ms),
                end_ms: old.end_ms.max(window.end_ms),
            },
            _ => window,
        });
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
}
impl Cadence {
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
            if let Some(next) = Window::control(c).and_then(|w| w.delay(now, fast_ms)) {
                delay = delay.min(next);
            }
            if c.state != 0 || (c.expires_ms != 0 && now >= c.expires_ms) {
                continue;
            }
            let start = c
                .not_before_ms
                .max(c.window_start_ms)
                .max(d.notified_ms.saturating_add(c.notice_ms));
            // Arm the admission time, not each constituent constraint: an
            // earlier wake must not consume a later, shorter maintenance window.
            if start > now
                && (c.window_end_ms == 0 || start < c.window_end_ms)
                && (c.expires_ms == 0 || start < c.expires_ms)
            {
                delay = delay.min(start - now);
            }
            if d.reason == "waiting for rollout eligibility" || d.reason == "ready for admission" {
                delay = delay.min(fast_ms);
            }
        }
        Duration::from_millis(delay.max(1))
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
            notified_ms: 0,
            reason: "not yet scheduled".into(),
            terminal: false,
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
        let failed = Cadence { failures: 10 };
        assert_eq!(failed.post_apply_delay(&cfg, Duration::from_secs(512), true).as_secs(), 512);
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
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 6000, None).as_secs(), 1);
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 7000, None).as_secs(), 1800);
        let d = deferred(Control {
            notice_ms: 2500,
            ..Default::default()
        });
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 0, None).as_millis(), 2500);
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
            },
            6000,
        );
        Window::remember(
            &mut state.check_window,
            &Control {
                window_start_ms: 5500,
                window_end_ms: 900000,
                ..Default::default()
            },
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
