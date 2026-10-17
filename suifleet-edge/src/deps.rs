//! Component dependency resolution. Pure and deterministic: the daemon loads
//! candidate data on request and reruns the resolver, which never touches the
//! network, the journal or installed components.
//!
//! - Requirements are space-separated comparators (`>=1.0.0 <2.0.0`, `^1.2.3`,
//!   `~1.2.3`, `=1.2.3`, a bare full version meaning exact, or `*`). Versions
//!   are full SemVer 2.0.0 without build metadata; `||` ranges are rejected.
//!   Prereleases only match a comparator naming the same core version with a
//!   prerelease, as in npm/Rust semver.
//! - HARD (the default) dependencies must be satisfied. A SOFT dependency that
//!   is not installed is omitted when no available version satisfies every
//!   requirement on it; once present it constrains versions like a hard one.
//! - Installed components that the plan does not replace keep constraining the
//!   components it does change, so a deployment cannot break them.
//! - An installed version satisfying all requirements is preferred over a
//!   change; otherwise the highest satisfying available version is chosen.
//! - Cycles, through either dependency type, are rejected. The install order
//!   puts dependencies first, ties broken by component name.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// Backtracking bound for one resolution attempt.
const SEARCH_LIMIT: usize = 10_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Kind {
    Hard,
    Soft,
}

/// One declared dependency, as recorded for installed components.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Dependency {
    pub requirement: String,
    pub kind: Kind,
}
pub type Dependencies = BTreeMap<String, Dependency>;

/// Catalog component names: 1–128 ASCII letters, digits, `.`, `_` or `-`.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
}

/// Parse a requirement; see the module documentation for the grammar.
pub fn requirement(text: &str) -> anyhow::Result<semver::VersionReq> {
    if text == "*" {
        return Ok(semver::VersionReq::STAR);
    }
    let mut comparators = Vec::new();
    for token in text.split(' ') {
        let (op, version) = [
            (">=", semver::Op::GreaterEq),
            ("<=", semver::Op::LessEq),
            (">", semver::Op::Greater),
            ("<", semver::Op::Less),
            ("=", semver::Op::Exact),
            ("^", semver::Op::Caret),
            ("~", semver::Op::Tilde),
        ]
        .iter()
        .find_map(|(prefix, op)| token.strip_prefix(prefix).map(|rest| (*op, rest)))
        .unwrap_or((semver::Op::Exact, token));
        let version = semver::Version::parse(version)
            .map_err(|e| anyhow::anyhow!("invalid version requirement {text:?}: {e}"))?;
        anyhow::ensure!(
            version.build.is_empty(),
            "invalid version requirement {text:?}: build metadata is not comparable"
        );
        comparators.push(semver::Comparator {
            op,
            major: version.major,
            minor: Some(version.minor),
            patch: Some(version.patch),
            pre: version.pre,
        });
    }
    Ok(semver::VersionReq { comparators })
}

/// Validated dependencies declared by a recipe for component `owner`.
pub fn declared(
    owner: &str,
    declared: Option<&BTreeMap<String, crate::protocol::ComponentDependency>>,
) -> anyhow::Result<Dependencies> {
    let mut result = Dependencies::new();
    for (name, dependency) in declared.into_iter().flatten() {
        anyhow::ensure!(valid_name(name), "invalid dependency name {name:?}");
        anyhow::ensure!(name != owner, "component {owner} depends on itself");
        requirement(&dependency.version_requirement)?;
        let kind = match dependency.dependency_type.as_deref() {
            None | Some("HARD") => Kind::Hard,
            Some("SOFT") => Kind::Soft,
            Some(other) => anyhow::bail!("unsupported dependency type {other:?}"),
        };
        result.insert(
            name.clone(),
            Dependency {
                requirement: dependency.version_requirement.clone(),
                kind,
            },
        );
    }
    Ok(result)
}

fn matches(requirement_text: &str, version: Option<&semver::Version>) -> bool {
    match (requirement(requirement_text), version) {
        (Ok(requirement), Some(version)) => requirement.matches(version),
        _ => false,
    }
}

/// The deployed component: always installed by the plan.
#[derive(Debug, Clone)]
pub struct Root {
    pub name: String,
    pub version: String,
    pub dependencies: Dependencies,
}

/// A component already on the device.
#[derive(Debug, Clone)]
pub struct Installed {
    pub version: String,
    pub dependencies: Dependencies,
    /// False when an install was started but never confirmed: never reused,
    /// but its requirements still constrain other components.
    pub confirmed: bool,
}

/// A candidate's authenticated details, loaded on request.
#[derive(Debug, Clone)]
pub enum Detail {
    /// Not installable here (for example no manifest for this platform).
    Unavailable(String),
    Ready(Dependencies),
}

/// A published version of a component.
#[derive(Debug, Clone)]
pub struct Candidate {
    pub version: String,
    pub detail: Option<Detail>,
}

/// Candidate versions by component name. A missing name has not been listed.
#[derive(Debug, Default)]
pub struct Pool {
    pub listings: BTreeMap<String, Vec<Candidate>>,
}
impl Pool {
    pub fn candidate_mut(&mut self, name: &str, version: &str) -> Option<&mut Candidate> {
        self.listings
            .get_mut(name)?
            .iter_mut()
            .find(|c| c.version == version)
    }
}

/// Data the resolver needs before it can continue.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Need {
    Listing(String),
    Detail { name: String, version: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Source {
    Root,
    /// Keep the installed version.
    Installed,
    /// Install this listed version.
    Candidate,
}

#[derive(Debug, Clone)]
pub struct Planned {
    pub name: String,
    pub version: String,
    pub source: Source,
    pub dependencies: Dependencies,
}
impl Planned {
    pub fn install(&self) -> bool {
        self.source != Source::Installed
    }
}

#[derive(Debug, Clone)]
pub struct Plan {
    /// Every selected component, dependencies before their dependents.
    pub order: Vec<Planned>,
    /// Soft dependencies left out because nothing satisfied them.
    pub omitted: Vec<String>,
}

#[derive(Debug)]
pub enum Step {
    Plan(Plan),
    Need(Need),
}

#[derive(Debug, Clone)]
enum Choice {
    Selected(Planned),
    Omitted,
}
impl Choice {
    fn satisfies(&self, dependency: &Dependency) -> bool {
        match self {
            Choice::Selected(planned) => matches(
                &dependency.requirement,
                semver::Version::parse(&planned.version).ok().as_ref(),
            ),
            Choice::Omitted => dependency.kind == Kind::Soft,
        }
    }
    fn dependencies(&self) -> Option<&Dependencies> {
        match self {
            Choice::Selected(planned) => Some(&planned.dependencies),
            Choice::Omitted => None,
        }
    }
}

enum Abort {
    Need(Need),
    Limit,
}

struct Search<'a> {
    installed: &'a BTreeMap<String, Installed>,
    pool: &'a Pool,
    steps: usize,
    /// The first failure found, reported if no alternative succeeds.
    conflict: Option<String>,
}

/// Resolve `root` against installed components and the candidate pool.
/// Returns the data still needed, a complete plan, or the rejection reason.
pub fn resolve(
    root: &Root,
    installed: &BTreeMap<String, Installed>,
    pool: &Pool,
) -> anyhow::Result<Step> {
    let mut search = Search {
        installed,
        pool,
        steps: 0,
        conflict: None,
    };
    let mut assigned = BTreeMap::new();
    assigned.insert(
        root.name.clone(),
        Choice::Selected(Planned {
            name: root.name.clone(),
            version: root.version.clone(),
            source: Source::Root,
            dependencies: root.dependencies.clone(),
        }),
    );
    match search.solve(&mut assigned) {
        Err(Abort::Need(need)) => Ok(Step::Need(need)),
        Err(Abort::Limit) => {
            anyhow::bail!("dependency resolution exceeded {SEARCH_LIMIT} search steps")
        }
        Ok(true) => {
            let order = topological(&assigned).map_err(|cycle| anyhow::anyhow!(cycle))?;
            let omitted = assigned
                .iter()
                .filter(|(_, choice)| matches!(choice, Choice::Omitted))
                .map(|(name, _)| name.clone())
                .collect();
            Ok(Step::Plan(Plan { order, omitted }))
        }
        Ok(false) => Err(anyhow::anyhow!(search
            .conflict
            .unwrap_or_else(|| "unresolvable component dependencies".into()))),
    }
}

impl Search<'_> {
    fn fail(&mut self, reason: String) -> bool {
        self.conflict.get_or_insert(reason);
        false
    }

    fn solve(&mut self, assigned: &mut BTreeMap<String, Choice>) -> Result<bool, Abort> {
        self.steps += 1;
        if self.steps > SEARCH_LIMIT {
            return Err(Abort::Limit);
        }
        let next = assigned
            .values()
            .filter_map(Choice::dependencies)
            .flat_map(|dependencies| dependencies.keys())
            .filter(|name| !assigned.contains_key(*name))
            .min()
            .cloned();
        let Some(name) = next else {
            return Ok(self.complete(assigned));
        };
        let requirements: Vec<(String, Dependency)> = assigned
            .iter()
            .filter_map(|(from, choice)| {
                Some((from.clone(), choice.dependencies()?.get(&name)?.clone()))
            })
            .collect();
        let accepts = |version: Option<&semver::Version>| {
            requirements
                .iter()
                .all(|(_, dependency)| matches(&dependency.requirement, version))
        };
        let described = requirements
            .iter()
            .map(|(from, d)| format!("{from} requires {:?} ({:?})", d.requirement, d.kind))
            .collect::<Vec<_>>()
            .join(", ");

        // `None` is a candidate whose details load only once every
        // preferred option has failed.
        let mut options: Vec<Option<Choice>> = Vec::new();
        let mut pending = Vec::new();
        let installed = self.installed.get(&name);
        if let Some(installed) = installed.filter(|i| i.confirmed) {
            if accepts(semver::Version::parse(&installed.version).ok().as_ref()) {
                options.push(Some(Choice::Selected(Planned {
                    name: name.clone(),
                    version: installed.version.clone(),
                    source: Source::Installed,
                    dependencies: installed.dependencies.clone(),
                })));
            }
        }
        let Some(listing) = self.pool.listings.get(&name) else {
            return Err(Abort::Need(Need::Listing(name)));
        };
        let mut listed: Vec<(semver::Version, &Candidate)> = listing
            .iter()
            .filter_map(|c| Some((semver::Version::parse(&c.version).ok()?, c)))
            .filter(|(version, _)| accepts(Some(version)))
            .collect();
        // Highest precedence first; the full string breaks build-metadata ties.
        listed.sort_by(|(a, ca), (b, cb)| b.cmp_precedence(a).then(ca.version.cmp(&cb.version)));
        let mut unavailable = Vec::new();
        for (_, candidate) in listed {
            if installed.is_some_and(|i| i.confirmed && i.version == candidate.version) {
                continue;
            }
            match &candidate.detail {
                None => {
                    options.push(None);
                    pending.push(candidate.version.clone());
                }
                Some(Detail::Unavailable(reason)) => {
                    unavailable.push(format!("{}: {reason}", candidate.version))
                }
                Some(Detail::Ready(dependencies)) => {
                    options.push(Some(Choice::Selected(Planned {
                        name: name.clone(),
                        version: candidate.version.clone(),
                        source: Source::Candidate,
                        dependencies: dependencies.clone(),
                    })))
                }
            }
        }
        if installed.is_none()
            && requirements
                .iter()
                .all(|(_, dependency)| dependency.kind == Kind::Soft)
        {
            options.push(Some(Choice::Omitted));
        }
        if options.is_empty() {
            let mut reason = format!("no available version of {name} satisfies: {described}");
            if let Some(installed) = installed {
                reason += &format!(
                    "; installed {} {}",
                    installed.version,
                    if installed.confirmed { "does not match" } else { "is unconfirmed" }
                );
            }
            if !unavailable.is_empty() {
                reason += &format!("; unavailable: {}", unavailable.join(", "));
            }
            return Ok(self.fail(reason));
        }
        let mut pending = pending.into_iter();
        let mut available = false;
        for choice in options {
            let Some(choice) = choice else {
                return Err(Abort::Need(Need::Detail {
                    name,
                    version: pending.next().unwrap(),
                }));
            };
            // Omission is for unavailability, never a way around a conflict or cycle.
            match choice {
                Choice::Omitted if available => continue,
                Choice::Omitted => {}
                Choice::Selected(_) => available = true,
            }
            // Requirements on components chosen earlier must still hold.
            if let Some(dependencies) = choice.dependencies() {
                let broken = dependencies.iter().find_map(|(dependency, d)| {
                    let chosen = assigned.get(dependency)?;
                    (!chosen.satisfies(d)).then(|| (dependency.clone(), d.clone()))
                });
                if let Some((dependency, d)) = broken {
                    let Choice::Selected(planned) = &choice else { unreachable!() };
                    self.fail(format!(
                        "{name} {} requires {dependency} {:?} ({:?}), which conflicts with the selected {}",
                        planned.version,
                        d.requirement,
                        d.kind,
                        describe(&dependency, &assigned[&dependency])
                    ));
                    continue;
                }
            }
            assigned.insert(name.clone(), choice);
            if self.solve(assigned)? {
                return Ok(true);
            }
            assigned.remove(&name);
        }
        Ok(false)
    }

    /// Accept a complete assignment only if no installed component it leaves
    /// in place is broken by a change, and it has no cycle.
    fn complete(&mut self, assigned: &BTreeMap<String, Choice>) -> bool {
        for (owner, installed) in self.installed {
            if assigned.contains_key(owner) {
                continue;
            }
            for (dependency, d) in &installed.dependencies {
                let Some(Choice::Selected(planned)) = assigned.get(dependency) else {
                    continue;
                };
                if planned.install() && !Choice::Selected(planned.clone()).satisfies(d) {
                    return self.fail(format!(
                        "installed {owner} {} requires {dependency} {:?} ({:?}), but the deployment selects {}",
                        installed.version, d.requirement, d.kind, planned.version
                    ));
                }
            }
        }
        match topological(assigned) {
            Ok(_) => true,
            Err(cycle) => self.fail(cycle),
        }
    }
}

fn describe(name: &str, choice: &Choice) -> String {
    match choice {
        Choice::Selected(planned) => format!("{name} {}", planned.version),
        Choice::Omitted => format!("omission of {name}"),
    }
}

/// Dependencies first; among ready components, the smallest name.
fn topological(assigned: &BTreeMap<String, Choice>) -> Result<Vec<Planned>, String> {
    let selected: BTreeMap<&String, &Planned> = assigned
        .iter()
        .filter_map(|(name, choice)| match choice {
            Choice::Selected(planned) => Some((name, planned)),
            Choice::Omitted => None,
        })
        .collect();
    let edges = |planned: &Planned| -> Vec<String> {
        planned
            .dependencies
            .keys()
            .filter(|d| selected.contains_key(*d))
            .cloned()
            .collect()
    };
    let mut done = BTreeSet::new();
    let mut order = Vec::new();
    while order.len() < selected.len() {
        let ready = selected.iter().find(|(name, planned)| {
            !done.contains(**name) && edges(planned).iter().all(|d| done.contains(d))
        });
        let Some((name, planned)) = ready else {
            // Every remaining component waits on another: follow the
            // smallest-name path until it repeats to report the cycle.
            let mut path: Vec<String> = Vec::new();
            let mut current = selected
                .keys()
                .find(|name| !done.contains(**name))
                .map(|name| (*name).clone())
                .unwrap();
            while !path.contains(&current) {
                path.push(current.clone());
                current = edges(selected[&current])
                    .into_iter()
                    .find(|d| !done.contains(d))
                    .unwrap();
            }
            let start = path.iter().position(|n| *n == current).unwrap();
            let mut cycle = path[start..].to_vec();
            cycle.push(current);
            return Err(format!("dependency cycle: {}", cycle.join(" -> ")));
        };
        done.insert((*name).clone());
        order.push((*planned).clone());
    }
    Ok(order)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn deps(list: &[(&str, &str, Kind)]) -> Dependencies {
        list.iter()
            .map(|(n, r, k)| {
                (
                    n.to_string(),
                    Dependency {
                        requirement: r.to_string(),
                        kind: *k,
                    },
                )
            })
            .collect()
    }
    use Kind::{Hard, Soft};

    /// A catalog of `name -> [(version, dependencies)]`, fully loaded.
    fn pool(entries: &[(&str, &[(&str, Dependencies)])]) -> Pool {
        let mut pool = Pool::default();
        for (name, versions) in entries {
            pool.listings.insert(
                name.to_string(),
                versions
                    .iter()
                    .map(|(v, d)| Candidate {
                        version: v.to_string(),
                        detail: Some(Detail::Ready(d.clone())),
                    })
                    .collect(),
            );
        }
        pool
    }
    fn root(dependencies: Dependencies) -> Root {
        Root {
            name: "app".into(),
            version: "1.0.0".into(),
            dependencies,
        }
    }
    fn plan(root: &Root, installed: &BTreeMap<String, Installed>, pool: &Pool) -> anyhow::Result<Plan> {
        match resolve(root, installed, pool)? {
            Step::Plan(plan) => Ok(plan),
            Step::Need(need) => panic!("unexpected need {need:?}"),
        }
    }
    fn names(plan: &Plan) -> Vec<String> {
        plan.order
            .iter()
            .map(|p| format!("{}@{}{}", p.name, p.version, if p.install() { "" } else { "=" }))
            .collect()
    }
    fn none() -> Dependencies {
        Dependencies::new()
    }

    #[test]
    fn requirement_grammar() {
        let ok = |r: &str, v: &str| requirement(r).unwrap().matches(&semver::Version::parse(v).unwrap());
        assert!(ok(">=1.0.0 <2.0.0", "1.9.9") && !ok(">=1.0.0 <2.0.0", "2.0.0"));
        assert!(ok("1.2.3", "1.2.3") && !ok("1.2.3", "1.2.4"), "bare is exact, not caret");
        assert!(ok("^0.3.1", "0.3.9") && !ok("^0.3.1", "0.4.0"));
        assert!(ok("~1.2.3", "1.2.9") && !ok("~1.2.3", "1.3.0"));
        assert!(ok("*", "7.0.0") && !ok("*", "7.0.0-rc.1"), "prereleases need an explicit comparator");
        assert!(ok(">=1.0.0-rc.1", "1.0.0-rc.2") && !ok(">=1.0.0-rc.1", "1.0.1-rc.1"));
        for bad in ["", "^1.2", "1.0.0 || 2.0.0", ">=1.0.0, <2.0.0", "1.0.0+b", "01.0.0", ">=1.0.0  <2.0.0", "x", "=>1.0.0"] {
            assert!(requirement(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn declared_dependencies_are_validated() {
        let raw = |entries: &[(&str, &str, Option<&str>)]| {
            entries
                .iter()
                .map(|(n, r, t)| {
                    (
                        n.to_string(),
                        crate::protocol::ComponentDependency {
                            version_requirement: r.to_string(),
                            dependency_type: t.map(str::to_owned),
                        },
                    )
                })
                .collect::<BTreeMap<_, _>>()
        };
        let parsed = declared("app", Some(&raw(&[("lib", "^1.0.0", None), ("opt", "*", Some("SOFT"))]))).unwrap();
        assert_eq!(parsed["lib"].kind, Hard);
        assert_eq!(parsed["opt"].kind, Soft);
        assert!(declared("app", None).unwrap().is_empty());
        assert!(declared("app", Some(&raw(&[("app", "*", None)]))).is_err(), "self dependency");
        assert!(declared("app", Some(&raw(&[("lib", "^1", None)]))).is_err());
        assert!(declared("app", Some(&raw(&[("lib", "*", Some("OPTIONAL"))]))).is_err());
        assert!(declared("app", Some(&raw(&[("l/b", "*", None)]))).is_err());
    }

    #[test]
    fn deterministic_dependencies_first_order() {
        // app -> {web, db}; web -> {log, db}; db -> log
        let pool = pool(&[
            ("web", &[("2.0.0", deps(&[("log", "^1.0.0", Hard), ("db", ">=3.0.0", Hard)]))]),
            ("db", &[("3.1.0", deps(&[("log", ">=1.1.0", Hard)]))]),
            ("log", &[("1.0.0", none()), ("1.2.0", none()), ("2.0.0", none())]),
        ]);
        let root = root(deps(&[("web", "^2.0.0", Hard), ("db", "^3.0.0", Hard)]));
        let first = plan(&root, &BTreeMap::new(), &pool).unwrap();
        assert_eq!(names(&first), ["log@1.2.0", "db@3.1.0", "web@2.0.0", "app@1.0.0"]);
        for _ in 0..3 {
            assert_eq!(names(&plan(&root, &BTreeMap::new(), &pool).unwrap()), names(&first));
        }
        // Independent ready components are ordered by name.
        let pool2 = self::pool(&[("b", &[("1.0.0", none())]), ("a", &[("1.0.0", none())])]);
        let root2 = self::root(deps(&[("b", "*", Hard), ("a", "*", Hard)]));
        assert_eq!(names(&plan(&root2, &BTreeMap::new(), &pool2).unwrap()), ["a@1.0.0", "b@1.0.0", "app@1.0.0"]);
    }

    #[test]
    fn backtracks_to_a_compatible_combination() {
        // Newest web needs log 2, but db pins log 1: web 1.5 is chosen.
        let pool = pool(&[
            ("web", &[("2.0.0", deps(&[("log", "^2.0.0", Hard)])), ("1.5.0", deps(&[("log", "^1.0.0", Hard)]))]),
            ("db", &[("1.0.0", deps(&[("log", "~1.0.0", Hard)]))]),
            ("log", &[("1.0.4", none()), ("2.0.0", none())]),
        ]);
        let root = root(deps(&[("db", "*", Hard), ("web", "*", Hard)]));
        assert_eq!(
            names(&plan(&root, &BTreeMap::new(), &pool).unwrap()),
            ["log@1.0.4", "db@1.0.0", "web@1.5.0", "app@1.0.0"]
        );
    }

    #[test]
    fn rejects_version_conflicts() {
        let pool = pool(&[
            ("a", &[("1.0.0", deps(&[("lib", "^1.0.0", Hard)]))]),
            ("b", &[("1.0.0", deps(&[("lib", "^2.0.0", Hard)]))]),
            ("lib", &[("1.4.0", none()), ("2.1.0", none())]),
        ]);
        let root = root(deps(&[("a", "*", Hard), ("b", "*", Hard)]));
        let error = plan(&root, &BTreeMap::new(), &pool).unwrap_err().to_string();
        assert!(error.contains("no available version of lib satisfies: a requires \"^1.0.0\" (Hard), b requires \"^2.0.0\" (Hard)"), "{error}");
        // A missing hard dependency is a rejection, never an omission.
        let error = plan(&self::root(deps(&[("absent", "*", Hard)])), &BTreeMap::new(), &self::pool(&[("absent", &[])]))
            .unwrap_err()
            .to_string();
        assert!(error.contains("no available version of absent"), "{error}");
    }

    #[test]
    fn rejects_cycles_including_soft_edges() {
        let pool = pool(&[
            ("a", &[("1.0.0", deps(&[("b", "*", Hard)]))]),
            ("b", &[("1.0.0", deps(&[("c", "*", Soft)]))]),
            ("c", &[("1.0.0", deps(&[("a", "*", Hard)]))]),
        ]);
        let error = plan(&root(deps(&[("a", "*", Hard)])), &BTreeMap::new(), &pool).unwrap_err().to_string();
        assert_eq!(error, "dependency cycle: a -> b -> c -> a");
        // Back to the deployed component.
        let pool = self::pool(&[("lib", &[("1.0.0", deps(&[("app", "*", Hard)]))])]);
        let error = plan(&root(deps(&[("lib", "*", Hard)])), &BTreeMap::new(), &pool).unwrap_err().to_string();
        assert_eq!(error, "dependency cycle: app -> lib -> app");
    }

    #[test]
    fn soft_dependencies_are_used_when_available_and_omitted_otherwise() {
        let root = root(deps(&[("metrics", "^1.0.0", Soft), ("lib", "*", Hard)]));
        let available = pool(&[("metrics", &[("1.3.0", none())]), ("lib", &[("1.0.0", none())])]);
        assert_eq!(names(&plan(&root, &BTreeMap::new(), &available).unwrap()), ["lib@1.0.0", "metrics@1.3.0", "app@1.0.0"]);
        let missing = pool(&[("metrics", &[("2.0.0", none())]), ("lib", &[("1.0.0", none())])]);
        // Available but conflicting with an earlier choice: rejected, not omitted.
        let conflicting = pool(&[("metrics", &[("1.0.0", deps(&[("lib", "^2.0.0", Hard)]))]), ("lib", &[("1.0.0", none())])]);
        assert!(plan(&root, &BTreeMap::new(), &conflicting).is_err());
        let result = plan(&root, &BTreeMap::new(), &missing).unwrap();
        assert_eq!(names(&result), ["lib@1.0.0", "app@1.0.0"]);
        assert_eq!(result.omitted, ["metrics"]);
        // A hard requirement elsewhere still needs it.
        let both = pool(&[("metrics", &[]), ("lib", &[("1.0.0", deps(&[("metrics", "*", Hard)]))])]);
        assert!(plan(&root, &BTreeMap::new(), &both).is_err());
        // An installed but incompatible soft dependency is a conflict, not an omission.
        let installed = BTreeMap::from([("metrics".to_string(), Installed { version: "2.0.0".into(), dependencies: none(), confirmed: true })]);
        assert!(plan(&root, &installed, &missing).is_err());
    }

    #[test]
    fn installed_versions_are_reused_and_protected() {
        let pool = pool(&[("lib", &[("1.0.0", none()), ("1.5.0", none()), ("2.0.0", none())])]);
        let installed = |version: &str, confirmed| {
            BTreeMap::from([("lib".to_string(), Installed { version: version.into(), dependencies: none(), confirmed })])
        };
        let root_v1 = root(deps(&[("lib", "^1.0.0", Hard)]));
        assert_eq!(names(&plan(&root_v1, &installed("1.0.0", true), &pool).unwrap()), ["lib@1.0.0=", "app@1.0.0"]);
        assert_eq!(names(&plan(&root_v1, &installed("2.0.0", true), &pool).unwrap()), ["lib@1.5.0", "app@1.0.0"]);
        assert_eq!(
            names(&plan(&root_v1, &installed("1.0.0", false), &pool).unwrap()),
            ["lib@1.5.0", "app@1.0.0"],
            "an unconfirmed install is replaced"
        );
        // An installed dependent outside the plan constrains what changes.
        let mut on_device = installed("1.0.0", true);
        on_device.insert("tool".into(), Installed { version: "3.0.0".into(), dependencies: deps(&[("lib", "<1.2.0", Hard)]), confirmed: true });
        let root_new = root(deps(&[("lib", ">=1.5.0", Hard)]));
        let error = plan(&root_new, &on_device, &pool).unwrap_err().to_string();
        assert!(error.contains("installed tool 3.0.0 requires lib"), "{error}");
        on_device.insert("app".into(), Installed { version: "0.9.0".into(), dependencies: none(), confirmed: true });
        on_device.insert("ui".into(), Installed { version: "1.0.0".into(), dependencies: deps(&[("app", "<1.0.0", Hard)]), confirmed: true });
        let error = plan(&root_v1, &on_device, &pool).unwrap_err().to_string();
        assert!(error.contains("installed ui 1.0.0 requires app"), "{error}");
    }

    #[test]
    fn requests_only_the_data_it_needs() {
        let root = root(deps(&[("lib", "^1.0.0", Hard)]));
        let mut pool = Pool::default();
        let mut needs = Vec::new();
        let result = loop {
            match resolve(&root, &BTreeMap::new(), &pool).unwrap() {
                Step::Plan(plan) => break plan,
                Step::Need(need) => {
                    needs.push(need.clone());
                    match need {
                        Need::Listing(name) => {
                            assert_eq!(name, "lib");
                            pool.listings.insert(
                                name,
                                ["0.9.0", "1.0.0", "1.1.0", "2.0.0"]
                                    .iter()
                                    .map(|v| Candidate { version: v.to_string(), detail: None })
                                    .collect(),
                            );
                        }
                        Need::Detail { name, version } => {
                            let detail = if version == "1.1.0" {
                                Detail::Unavailable("no linux/x86_64 manifest".into())
                            } else {
                                Detail::Ready(none())
                            };
                            pool.candidate_mut(&name, &version).unwrap().detail = Some(detail);
                        }
                    }
                }
            }
        };
        assert_eq!(names(&result), ["lib@1.0.0", "app@1.0.0"]);
        assert_eq!(
            needs,
            [
                Need::Listing("lib".into()),
                Need::Detail { name: "lib".into(), version: "1.1.0".into() },
                Need::Detail { name: "lib".into(), version: "1.0.0".into() },
            ],
            "versions outside the requirement are never loaded"
        );
    }

    #[test]
    fn search_is_bounded() {
        // Every combination of 12 two-version components fails at the end.
        let mut entries: Vec<(String, Vec<(String, Dependencies)>)> = Vec::new();
        let mut root_deps = Vec::new();
        for i in 0..12 {
            let name = format!("c{i:02}");
            entries.push((name.clone(), vec![("1.0.0".into(), deps(&[("z", "^1.0.0", Hard)])), ("1.1.0".into(), deps(&[("z", "^1.0.0", Hard)]))]));
            root_deps.push((name, "*".to_string(), Hard));
        }
        let mut pool = Pool::default();
        for (name, versions) in entries {
            pool.listings.insert(name, versions.into_iter().map(|(v, d)| Candidate { version: v, detail: Some(Detail::Ready(d)) }).collect());
        }
        pool.listings.insert("z".into(), vec![Candidate { version: "2.0.0".into(), detail: Some(Detail::Ready(none())) }]);
        let root_deps: Vec<(&str, &str, Kind)> = root_deps.iter().map(|(n, r, k)| (n.as_str(), r.as_str(), *k)).collect();
        let error = plan(&root(deps(&root_deps)), &BTreeMap::new(), &pool).unwrap_err().to_string();
        assert!(error.contains("search steps") || error.contains("no available version of z"), "{error}");
    }
}
