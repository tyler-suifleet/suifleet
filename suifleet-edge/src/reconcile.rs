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
//! A plan is the name-ordered list of desired deployments the journal has no
//! outcome for. Steps run one at a time with intent journaled first, so a
//! crash never re-executes an updater; the first step that does not apply
//! stops the plan, and the same desired state is not retried (see
//! `fingerprint`) until it changes.
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

/// Desired deployments still to apply, in deterministic (name) order.
pub fn plan(effective: &Effective, done: impl Fn(&str) -> bool) -> Vec<(String, String)> {
    effective
        .components
        .iter()
        .filter(|(_, deployment)| !done(deployment))
        .map(|(name, deployment)| (name.clone(), deployment.clone()))
        .collect()
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
            plan(&result, |_| false),
            vec![("agent".to_string(), "0x2".to_string())]
        );
    }

    #[test]
    fn only_the_current_revision_is_planned() {
        // Offline through revisions 1..3: only revision 3's deployment runs.
        let latest = effective(&[group("0xa", 3, &[("firmware", "0x13")])]);
        let steps = plan(&latest, |_| false);
        assert_eq!(steps, vec![("firmware".to_string(), "0x13".to_string())]);
        // Already applied (or failed) deployments are never re-run.
        assert!(plan(&latest, |d| id(d) == id("0x0013")).is_empty());
    }

    #[test]
    fn plans_are_name_ordered_and_skip_done_steps() {
        let result = effective(&[group(
            "0xa",
            1,
            &[("zeta", "0x3"), ("alpha", "0x1"), ("mid", "0x2")],
        )]);
        let names: Vec<_> = plan(&result, |d| d == "0x2")
            .into_iter()
            .map(|(n, _)| n)
            .collect();
        assert_eq!(names, ["alpha", "zeta"]);
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
