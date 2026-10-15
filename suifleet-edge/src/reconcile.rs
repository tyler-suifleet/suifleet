//! Desired-state reconciliation (SF-013): pure planning, no I/O.
//!
//! Each group a device belongs to has a revisioned desired state on chain:
//! component name → DeploymentRecord. The device follows the union of the
//! current revisions of its groups:
//!
//! - only the deployment each group *currently* names is ever applied, so an
//!   offline device never works through superseded versions;
//! - a device that joins a group picks up its current state; one that leaves
//!   stops following it (the component is retired from management; there is
//!   no uninstall step until component lifecycles exist, SF-014);
//! - when groups name different deployments for one component, that is a
//!   conflict: the component is left as installed until they agree.
//!
//! A plan compares each desired deployment, in name order, with what the
//! component currently runs (the deployment last started for it, one-shot or
//! desired) rather than with execution history. Steps run one at a time with
//! intent journaled first, so a crash never re-executes an updater. The step
//! is journaled before its intent, with the desired state (`fingerprint`) it
//! was started for; until it has applied, later steps stay stopped, also
//! across a crash. That desired state is not retried until it changes, and a
//! newer one waits only while the step's update is still in flight
//! (`step_holds`).
//!
//! A deployment ID is also its execution identity (journal entry, firmware
//! request, on-chain receipt), so a deployment never runs twice. Selecting
//! one again after the component moved on (A → B → A, or a one-shot update
//! after a desired one) is reported as unsupported and left as installed: a
//! new deployment record of that version is needed.
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;

/// One group's current desired state, read from chain.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroupDesired {
    pub group: String,
    /// 0 for a group that never had desired state.
    pub revision: u64,
    /// Component name → DeploymentRecord ID.
    pub components: BTreeMap<String, String>,
}

/// What this device should run, combined over all its groups.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct Effective {
    /// Component name → the deployment every naming group agrees on.
    pub components: BTreeMap<String, String>,
    /// Component name → (group → deployment) where groups disagree.
    pub conflicts: BTreeMap<String, BTreeMap<String, String>>,
}

/// Canonical form of an object ID, for comparisons only: reads and journal
/// keys keep the chain's own spelling.
pub fn id(value: &str) -> String {
    let hex = value.trim_start_matches("0x").to_ascii_lowercase();
    format!("0x{hex:0>64}")
}

pub fn effective(groups: &[GroupDesired]) -> Effective {
    let mut named: BTreeMap<String, BTreeMap<String, String>> = BTreeMap::new();
    for group in groups {
        for (component, deployment) in &group.components {
            named
                .entry(component.clone())
                .or_default()
                .insert(group.group.clone(), deployment.clone());
        }
    }
    let mut result = Effective::default();
    for (component, sources) in named {
        let mut distinct: Vec<String> = sources.values().map(|d| id(d)).collect();
        distinct.sort();
        distinct.dedup();
        if distinct.len() == 1 {
            let deployment = sources.values().next().expect("named by a group").clone();
            result.components.insert(component, deployment);
        } else {
            result.conflicts.insert(component, sources);
        }
    }
    result
}

/// Identifies a desired state; a plan that failed for it is not retried
/// until it changes.
pub fn fingerprint(effective: &Effective) -> String {
    let canonical = |map: &BTreeMap<String, String>| -> BTreeMap<String, String> {
        map.iter().map(|(k, v)| (k.clone(), id(v))).collect()
    };
    let conflicts: BTreeMap<_, _> = effective
        .conflicts
        .iter()
        .map(|(name, sources)| {
            let sources: BTreeMap<String, String> =
                sources.iter().map(|(g, d)| (id(g), id(d))).collect();
            (name.clone(), sources)
        })
        .collect();
    let encoded = serde_json::to_vec(&(canonical(&effective.components), conflicts))
        .expect("maps of strings always serialize");
    hex::encode(Sha256::digest(encoded))
}

/// What the journal knows about one deployment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum History {
    Never,
    Applied,
    /// Failed, indeterminate or still pending: never re-run.
    NotApplied,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Step {
    /// Never started here: run it.
    Apply(String, String),
    /// Applied here, and the journal predates tracking of what the component
    /// currently runs: record it as current without running it.
    Adopt(String, String),
    /// Applied here, but the component has run something else since.
    Reselected(String, String),
}

/// Steps toward the effective desired state, in deterministic (name) order.
/// Components already at their desired deployment, and desired deployments
/// that did not apply here, have none.
pub fn plan(
    effective: &Effective,
    current: &BTreeMap<String, String>,
    history: impl Fn(&str) -> History,
) -> Vec<Step> {
    let mut steps = Vec::new();
    for (name, deployment) in &effective.components {
        let last = current.get(name);
        if last.is_some_and(|last| id(last) == id(deployment)) {
            continue;
        }
        let step = (name.clone(), deployment.clone());
        match (history(deployment), last) {
            (History::Never, _) => steps.push(Step::Apply(step.0, step.1)),
            (History::NotApplied, _) => {}
            (History::Applied, None) => steps.push(Step::Adopt(step.0, step.1)),
            (History::Applied, Some(_)) => steps.push(Step::Reselected(step.0, step.1)),
        }
    }
    steps
}

/// Whether a journaled step that has not applied still stops the plan for
/// desired state `now`. It always does for the desired state it was started
/// for (`owner`); a newer desired state waits only until the step's update
/// has settled (Failed, or indeterminate), however many polls or restarts
/// that takes, and is then planned afresh.
pub fn step_holds(owner: &str, now: &str, settled: bool) -> bool {
    owner == now || !settled
}

/// Managed components no group wants any more. A conflicted component is
/// still wanted (by someone), so it is not retired.
pub fn retired(managed: &BTreeMap<String, String>, effective: &Effective) -> Vec<String> {
    managed
        .keys()
        .filter(|name| {
            !effective.components.contains_key(*name) && !effective.conflicts.contains_key(*name)
        })
        .cloned()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn group(id: &str, revision: u64, components: &[(&str, &str)]) -> GroupDesired {
        GroupDesired {
            group: id.into(),
            revision,
            components: components
                .iter()
                .map(|(name, deployment)| (name.to_string(), deployment.to_string()))
                .collect(),
        }
    }

    #[test]
    fn union_of_groups_with_agreement() {
        let result = effective(&[
            group("0xa", 2, &[("firmware", "0x1"), ("agent", "0x2")]),
            group("0xb", 1, &[("firmware", "0x01"), ("logger", "0x3")]),
        ]);
        assert!(result.conflicts.is_empty());
        assert_eq!(result.components.len(), 3);
        assert_eq!(id(&result.components["firmware"]), id("0x1"));
    }

    #[test]
    fn disagreement_is_a_conflict_not_a_choice() {
        let result = effective(&[
            group("0xa", 1, &[("firmware", "0x1"), ("agent", "0x2")]),
            group("0xb", 4, &[("firmware", "0x9")]),
        ]);
        assert_eq!(result.components.keys().collect::<Vec<_>>(), ["agent"]);
        assert_eq!(result.conflicts["firmware"].len(), 2);
        // Conflicted components are neither planned nor retired.
        let managed = BTreeMap::from([("firmware".to_string(), id("0x1"))]);
        assert!(retired(&managed, &result).is_empty());
        assert_eq!(
            plan(&result, &BTreeMap::new(), |_| History::Never),
            vec![Step::Apply("agent".into(), "0x2".into())]
        );
    }

    fn applied_only(applied: &'static [&'static str]) -> impl Fn(&str) -> History {
        move |d| match applied.iter().any(|a| id(a) == id(d)) {
            true => History::Applied,
            false => History::Never,
        }
    }

    #[test]
    fn only_the_current_revision_is_planned() {
        // Offline through revisions 1..3: only revision 3's deployment runs.
        let latest = effective(&[group("0xa", 3, &[("firmware", "0x13")])]);
        let steps = plan(&latest, &BTreeMap::new(), |_| History::Never);
        assert_eq!(steps, vec![Step::Apply("firmware".into(), "0x13".into())]);
        // Already the component's current deployment: nothing to do, whatever
        // its outcome (a failed one is not retried).
        let current = BTreeMap::from([("firmware".to_string(), "0x0013".to_string())]);
        for history in [History::Applied, History::NotApplied] {
            assert!(plan(&latest, &current, |_| history).is_empty());
        }
    }

    #[test]
    fn plans_are_name_ordered_and_skip_converged_steps() {
        let result = effective(&[group(
            "0xa",
            1,
            &[("zeta", "0x3"), ("alpha", "0x1"), ("mid", "0x2")],
        )]);
        let current = BTreeMap::from([("mid".to_string(), "0x2".to_string())]);
        let names: Vec<_> = plan(&result, &current, applied_only(&["0x2"]))
            .into_iter()
            .map(|step| match step {
                Step::Apply(name, _) => name,
                other => panic!("unexpected {other:?}"),
            })
            .collect();
        assert_eq!(names, ["alpha", "zeta"]);
    }

    #[test]
    fn step_stops_its_own_desired_state_and_newer_ones_until_settled() {
        // A failed or indeterminate step: its own desired state stays stopped.
        assert!(step_holds("a", "a", true));
        // Still in flight: every desired state waits for it.
        assert!(step_holds("a", "a", false));
        assert!(step_holds("a", "b", false));
        // Settled under a newer desired state: that one is planned. This holds
        // on every later poll, not only on the one that first saw `b`.
        assert!(!step_holds("a", "b", true));
    }

    #[test]
    fn history_is_not_installation() {
        // A applied, then B; a revision selecting A again must not be taken as
        // converged: A cannot run twice, so it is reported, not recorded.
        let back = effective(&[group("0xa", 3, &[("firmware", "0xa")])]);
        let current = BTreeMap::from([("firmware".to_string(), "0xb".to_string())]);
        assert_eq!(
            plan(&back, &current, applied_only(&["0xa", "0xb"])),
            vec![Step::Reselected("firmware".into(), "0xa".into())]
        );
        // Same after a one-shot update of the component.
        let current = BTreeMap::from([("firmware".to_string(), "0xc".to_string())]);
        assert_eq!(
            plan(&back, &current, applied_only(&["0xa", "0xc"])),
            vec![Step::Reselected("firmware".into(), "0xa".into())]
        );
        // A desired deployment that failed here is not retried either way.
        assert!(plan(&back, &current, |_| History::NotApplied).is_empty());
        // A journal that predates `current` adopts what already applied.
        assert_eq!(
            plan(&back, &BTreeMap::new(), applied_only(&["0xa"])),
            vec![Step::Adopt("firmware".into(), "0xa".into())]
        );
        // A new version is applied normally.
        let next = effective(&[group("0xa", 4, &[("firmware", "0xd")])]);
        assert_eq!(
            plan(&next, &current, applied_only(&["0xa", "0xc"])),
            vec![Step::Apply("firmware".into(), "0xd".into())]
        );
    }

    #[test]
    fn leaving_a_group_or_removing_a_component_retires_it() {
        let managed = BTreeMap::from([
            ("firmware".to_string(), id("0x1")),
            ("agent".to_string(), id("0x2")),
        ]);
        let now = effective(&[group("0xa", 2, &[("firmware", "0x1")])]);
        assert_eq!(retired(&managed, &now), ["agent"]);
        assert_eq!(retired(&managed, &effective(&[])), ["agent", "firmware"]);
    }

    #[test]
    fn fingerprint_tracks_the_desired_state() {
        let a = effective(&[group("0xa", 1, &[("firmware", "0x1")])]);
        let same = effective(&[group("0xA", 7, &[("firmware", "0x01")])]);
        let other = effective(&[group("0xa", 2, &[("firmware", "0x2")])]);
        assert_eq!(
            fingerprint(&a),
            fingerprint(&same),
            "revision numbers alone do not change the plan"
        );
        assert_ne!(fingerprint(&a), fingerprint(&other));
        assert_ne!(fingerprint(&a), fingerprint(&effective(&[])));
    }
}
