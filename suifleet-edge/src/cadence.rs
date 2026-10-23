//! Local wakeups never authorize an apply: every wake performs fresh admission reads.
use crate::{config::RuntimeConfig, control::Deferred};
use rand::Rng;
use std::time::Duration;

#[derive(Default)]
pub struct Cadence {
    failures: u32,
}
impl Cadence {
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
    ) -> Duration {
        self.delay_with(config, controls, now, &mut rand::thread_rng())
    }
    fn delay_with<'a>(
        &self,
        config: &RuntimeConfig,
        controls: impl Iterator<Item = &'a Deferred>,
        now: u64,
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
        for d in controls.filter(|d| !d.terminal) {
            let c = &d.control;
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
            if (c.window_end_ms > now && c.window_start_ms <= now)
                || d.reason == "waiting for rollout eligibility"
                || d.reason == "ready for admission"
            {
                delay = delay.min(config.rollout_poll_seconds.min(config.poll_seconds) * 1000);
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
    fn defaults_fast_windows_and_short_local_deadlines() {
        let cfg = RuntimeConfig::default();
        let c = Cadence::default();
        assert_eq!(c.delay(&cfg, [].iter(), 0).as_secs(), 1800);
        let d = deferred(Control {
            not_before_ms: 5000,
            window_start_ms: 6000,
            window_end_ms: 7000,
            ..Default::default()
        });
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 0).as_millis(), 6000);
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 6000).as_secs(), 60);
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 7000).as_secs(), 1800);
        let d = deferred(Control {
            notice_ms: 2500,
            ..Default::default()
        });
        assert_eq!(c.delay(&cfg, [&d].into_iter(), 0).as_millis(), 2500);
    }
    #[test]
    fn fleet_failures_spread_and_recovery_resets() {
        let cfg = RuntimeConfig::default();
        let mut c = Cadence::default();
        let mut rng = StdRng::seed_from_u64(123);
        c.record(false);
        let delays: std::collections::HashSet<_> = (0..1000)
            .map(|_| c.delay_with(&cfg, [].iter(), 0, &mut rng).as_millis())
            .collect();
        assert!(delays.len() > 500);
        for _ in 0..100 {
            c.record(false);
        }
        let delay = c.delay(&cfg, [].iter(), 0).as_secs();
        assert!((1800..=3600).contains(&delay));
        c.record(true);
        assert_eq!(c.delay(&cfg, [].iter(), 0).as_secs(), 1800);
    }
}
