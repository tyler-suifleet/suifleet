//! Component configuration (SF-015): recipe defaults, reset/merge updates,
//! validation before apply, persistence and device-local confidential values.
//!
//! A recipe (schema 1.2) carries `configuration.defaults`, an update
//! (`reset` JSON pointers, then a `merge` object) and `confidential`
//! pointers. Each install computes the component's next effective
//! configuration from the one last committed for it:
//!
//! 1. Defaults fill in what is missing; existing values are kept, so a new
//!    version's changed default applies only once reset.
//! 2. Each `reset` pointer (RFC 6901) takes the default's value there, or is
//!    removed when the defaults have none; `""` resets everything.
//! 3. `merge` is merged recursively: objects merge key by key, any other value
//!    (array, scalar, null) replaces what was there.
//! 4. Confidential pointers are removed: their values never come from a
//!    (public) recipe, only from the device's overlay file.
//!
//! The committed record is public data: it holds no confidential values and
//! may be shown in diagnostics. What a component receives is materialized
//! from it plus the overlay, into a 0600 file. Errors name pointers, never
//! values.
//!
//! Layout under `<state_dir>/configuration/<name>/`: `committed.json`, the
//! configuration of the version the component now runs, and `staged.json`,
//! the one an in-flight deployment is installing. A staged record is
//! committed once that install is confirmed and discarded otherwise, also
//! after a crash ([`Store::settle`]).
use crate::protocol::Configuration;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::{
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
};

/// Shared with the frontend (`CONFIGURATION_LIMITS` in recipe-schema.ts).
pub const MAX_DEPTH: usize = 32;
pub const MAX_VALUES: usize = 4096;
const MAX_POINTERS: usize = 64;
const MAX_POINTER_LENGTH: usize = 1024;
const MAX_OVERLAY_BYTES: u64 = 1024 * 1024;
/// Exit status of a validate command that defers the update (EX_TEMPFAIL).
pub const DEFER_EXIT: i32 = 75;

/// RFC 6901 reference tokens of `pointer`; none for `""`, the whole document.
pub fn tokens(pointer: &str) -> anyhow::Result<Vec<String>> {
    anyhow::ensure!(
        pointer.encode_utf16().count() <= MAX_POINTER_LENGTH,
        "configuration pointer longer than {MAX_POINTER_LENGTH}"
    );
    if pointer.is_empty() {
        return Ok(Vec::new());
    }
    let Some(rest) = pointer.strip_prefix('/') else {
        anyhow::bail!("configuration pointer {pointer:?} must be empty or start with /");
    };
    rest.split('/')
        .map(|token| {
            let mut chars = token.chars();
            while let Some(c) = chars.next() {
                anyhow::ensure!(
                    c != '~' || matches!(chars.next(), Some('0' | '1')),
                    "configuration pointer {pointer:?} has an invalid ~ escape"
                );
            }
            Ok(token.replace("~1", "/").replace("~0", "~"))
        })
        .collect()
}

/// The value at `tokens`, if any. Passing through anything but an object
/// (an array element, or into a scalar) is an error: configuration pointers
/// address object members only.
fn lookup<'a>(document: &'a Map<String, Value>, tokens: &[String], pointer: &str) -> anyhow::Result<Option<&'a Value>> {
    let Some((last, parents)) = tokens.split_last() else {
        anyhow::bail!("configuration pointer {pointer:?} addresses the whole document");
    };
    let mut node = document;
    for token in parents {
        match node.get(token) {
            None => return Ok(None),
            Some(Value::Object(child)) => node = child,
            Some(_) => anyhow::bail!("configuration pointer {pointer:?} passes through a non-object value"),
        }
    }
    Ok(node.get(last))
}

/// The object holding the last token, created (as objects) where missing.
fn parent_mut<'a>(document: &'a mut Map<String, Value>, tokens: &[String], pointer: &str) -> anyhow::Result<&'a mut Map<String, Value>> {
    let mut node = document;
    for token in &tokens[..tokens.len() - 1] {
        let child = node.entry(token.clone()).or_insert_with(|| Value::Object(Map::new()));
        let Value::Object(child) = child else {
            anyhow::bail!("configuration pointer {pointer:?} passes through a non-object value");
        };
        node = child;
    }
    Ok(node)
}

fn set(document: &mut Map<String, Value>, pointer: &str, value: Value) -> anyhow::Result<()> {
    let tokens = tokens(pointer)?;
    anyhow::ensure!(!tokens.is_empty(), "configuration pointer {pointer:?} addresses the whole document");
    parent_mut(document, &tokens, pointer)?.insert(tokens.last().unwrap().clone(), value);
    Ok(())
}

fn remove(document: &mut Map<String, Value>, pointer: &str) -> anyhow::Result<()> {
    let tokens = tokens(pointer)?;
    if lookup(document, &tokens, pointer)?.is_some() {
        parent_mut(document, &tokens, pointer)?.remove(tokens.last().unwrap());
    }
    Ok(())
}

/// Add every default that `target` does not have; keep what it has.
pub fn fill(target: &mut Map<String, Value>, defaults: &Map<String, Value>) {
    for (key, default) in defaults {
        match (target.get_mut(key), default) {
            (None, _) => {
                target.insert(key.clone(), default.clone());
            }
            (Some(Value::Object(existing)), Value::Object(default)) => fill(existing, default),
            _ => {}
        }
    }
}

/// Merge `patch` into `target`: objects recursively, anything else replaces.
pub fn merge(target: &mut Map<String, Value>, patch: &Map<String, Value>) {
    for (key, value) in patch {
        match (target.get_mut(key), value) {
            (Some(Value::Object(existing)), Value::Object(patch)) => merge(existing, patch),
            _ => {
                target.insert(key.clone(), value.clone());
            }
        }
    }
}

/// Reset `pointer` to its default, or remove it when there is none.
pub fn reset(effective: &mut Map<String, Value>, defaults: &Map<String, Value>, pointer: &str) -> anyhow::Result<()> {
    let tokens = tokens(pointer)?;
    if tokens.is_empty() {
        *effective = defaults.clone();
        return Ok(());
    }
    match lookup(defaults, &tokens, pointer)? {
        Some(default) => set(effective, pointer, default.clone()),
        None => remove(effective, pointer),
    }
}

/// Values and nesting depth (the document itself is depth 1) are bounded.
fn check_limits(document: &Map<String, Value>, what: &str) -> anyhow::Result<()> {
    fn walk(value: &Value, depth: usize, values: &mut usize) -> bool {
        *values += 1;
        if *values > MAX_VALUES || depth > MAX_DEPTH {
            return false;
        }
        match value {
            Value::Object(map) => map.values().all(|v| walk(v, depth + 1, values)),
            Value::Array(items) => items.iter().all(|v| walk(v, depth + 1, values)),
            _ => true,
        }
    }
    let mut values = 1;
    anyhow::ensure!(
        document.values().all(|v| walk(v, 2, &mut values)),
        "{what} exceeds {MAX_VALUES} values or depth {MAX_DEPTH}"
    );
    Ok(())
}

/// Static checks of a recipe's configuration, as the frontend makes them
/// before upload: pointer syntax, limits, and no confidential value in it.
pub fn validate_recipe(configuration: &Configuration) -> anyhow::Result<()> {
    let reset = configuration.reset.as_deref().unwrap_or_default();
    let confidential = configuration.confidential.as_deref().unwrap_or_default();
    anyhow::ensure!(
        reset.len() <= MAX_POINTERS && confidential.len() <= MAX_POINTERS,
        "too many configuration pointers"
    );
    for pointer in reset {
        tokens(pointer)?;
    }
    for pointer in confidential {
        anyhow::ensure!(!tokens(pointer)?.is_empty(), "the whole configuration cannot be confidential");
    }
    for (name, document) in [("defaults", &configuration.defaults), ("merge", &configuration.merge)] {
        let Some(document) = document else { continue };
        check_limits(document, &format!("configuration {name}"))?;
        // Recipes are public: a confidential value only ever comes from the device.
        for pointer in confidential {
            anyhow::ensure!(
                lookup(document, &tokens(pointer)?, pointer)?.is_none(),
                "confidential {pointer} must not have a value in a public recipe ({name})"
            );
        }
    }
    Ok(())
}

/// A component's configuration as committed or staged. Public: confidential
/// values are never part of it.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Record {
    pub deployment: String,
    pub version: String,
    pub defaults: Map<String, Value>,
    pub effective: Map<String, Value>,
    #[serde(default)]
    pub confidential: Vec<String>,
}

/// The next effective configuration after `previous` (none for a first
/// install), for a recipe's configuration.
pub fn next(previous: Option<&Map<String, Value>>, recipe: &Configuration) -> anyhow::Result<Map<String, Value>> {
    validate_recipe(recipe)?;
    let defaults = recipe.defaults.clone().unwrap_or_default();
    let mut effective = previous.cloned().unwrap_or_default();
    fill(&mut effective, &defaults);
    for pointer in recipe.reset.as_deref().unwrap_or_default() {
        reset(&mut effective, &defaults, pointer)?;
    }
    merge(&mut effective, recipe.merge.as_ref().unwrap_or(&Map::new()));
    // A value an earlier version kept in public is dropped once confidential.
    for pointer in recipe.confidential.as_deref().unwrap_or_default() {
        remove(&mut effective, pointer)?;
    }
    check_limits(&effective, "effective configuration")?;
    Ok(effective)
}

/// The device-local values of `name`: `<dir>/<name>.json`, an object of
/// JSON pointer → value. It must not be accessible to other users.
pub fn overlay(dir: &Path, name: &str) -> anyhow::Result<Map<String, Value>> {
    let path = dir.join(format!("{name}.json"));
    let metadata = match std::fs::metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Map::new()),
        Err(error) => return Err(anyhow::anyhow!("cannot read configuration overlay of {name}: {error}")),
    };
    anyhow::ensure!(
        metadata.permissions().mode() & 0o007 == 0,
        "configuration overlay of {name} is accessible to other users; restrict it (chmod o-rwx)"
    );
    anyhow::ensure!(metadata.len() <= MAX_OVERLAY_BYTES, "configuration overlay of {name} is too large");
    let bytes = std::fs::read(&path)?;
    // The overlay holds secrets: a parse error must not quote its content.
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| anyhow::anyhow!("configuration overlay of {name} is not valid JSON"))?;
    let Value::Object(map) = value else {
        anyhow::bail!("configuration overlay of {name} must be an object of JSON pointer to value");
    };
    for pointer in map.keys() {
        anyhow::ensure!(!tokens(pointer)?.is_empty(), "configuration overlay of {name} cannot replace the whole document");
    }
    Ok(map)
}

/// What the component receives: the public configuration with the overlay
/// set on top. Every confidential pointer must be provisioned.
pub fn materialize(record: &Record, overlay: &Map<String, Value>) -> anyhow::Result<Value> {
    for pointer in &record.confidential {
        anyhow::ensure!(
            overlay.contains_key(pointer),
            "confidential configuration {pointer} is not provisioned in the device overlay"
        );
    }
    let mut document = record.effective.clone();
    for (pointer, value) in overlay {
        set(&mut document, pointer, value.clone())?;
    }
    Ok(Value::Object(document))
}

/// Write `value` to `path` (mode 0600) atomically.
pub fn write_private(path: &Path, value: &impl Serialize) -> anyhow::Result<()> {
    let temporary = path.with_extension("tmp");
    let _ = std::fs::remove_file(&temporary);
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(&serde_json::to_vec_pretty(value)?)?;
    file.sync_all()?;
    std::fs::rename(&temporary, path)?;
    sync_parent(path)
}

/// Make `path`'s directory entry durable.
fn sync_parent(path: &Path) -> anyhow::Result<()> {
    let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
    #[cfg(test)]
    SYNCED.with(|synced| synced.borrow_mut().push(parent.to_owned()));
    std::fs::File::open(parent)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
thread_local! {
    /// Directories `sync_parent` synced on this thread, oldest first.
    pub static SYNCED: std::cell::RefCell<Vec<PathBuf>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// `create_dir_all` for `path` inside `base`, with every directory between
/// them made durable in its parent (`base` itself included), so a record
/// written inside survives a power loss. Directories that already existed are
/// synced too: an earlier, non-durable creation (a component's log or validate
/// work directory) may not be on disk yet.
pub fn create_dir_durable(base: &Path, path: &Path) -> anyhow::Result<()> {
    anyhow::ensure!(path.starts_with(base) && path != base, "{} is not inside {}", path.display(), base.display());
    std::fs::create_dir_all(path)?;
    let dirs: Vec<&Path> = path.ancestors().take_while(|dir| *dir != base).collect();
    for dir in dirs.into_iter().rev() {
        sync_parent(dir)?;
    }
    Ok(())
}

fn read<T: for<'de> Deserialize<'de>>(path: &Path) -> anyhow::Result<Option<T>> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub struct Store {
    root: PathBuf,
    pub overlay_dir: PathBuf,
}

/// What became of a staged record's deployment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Settled {
    /// Its install is confirmed (or the supervisor committed its version).
    Committed,
    /// Its outcome is still unknown (a pending privileged update).
    Pending,
    /// It did not install.
    Abandoned,
}

impl Store {
    pub fn open(state_dir: &Path, overlay_dir: &Path) -> anyhow::Result<Self> {
        let root = state_dir.join("configuration");
        std::fs::create_dir_all(&root)?;
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700))?;
        Ok(Self { root, overlay_dir: overlay_dir.to_owned() })
    }

    fn dir(&self, name: &str) -> anyhow::Result<PathBuf> {
        anyhow::ensure!(crate::deps::valid_name(name), "component name is not a safe path segment");
        Ok(self.root.join(name))
    }

    pub fn committed(&self, name: &str) -> anyhow::Result<Option<Record>> {
        read(&self.dir(name)?.join("committed.json"))
    }

    pub fn staged(&self, name: &str) -> anyhow::Result<Option<Record>> {
        read(&self.dir(name)?.join("staged.json"))
    }

    pub fn stage(&self, name: &str, record: &Record) -> anyhow::Result<()> {
        let dir = self.dir(name)?;
        // Durable before the install it precedes starts (see settle).
        create_dir_durable(self.root.parent().unwrap_or(&self.root), &dir)?;
        write_private(&dir.join("staged.json"), record)
    }

    /// Make the record staged for `deployment` the committed one.
    pub fn commit(&self, name: &str, deployment: &str) -> anyhow::Result<()> {
        let dir = self.dir(name)?;
        if self.staged(name)?.is_some_and(|r| r.deployment == deployment) {
            std::fs::rename(dir.join("staged.json"), dir.join("committed.json"))?;
            sync_parent(&dir.join("committed.json"))?;
        }
        Ok(())
    }

    /// Make `record` the committed one of `name` again, or none: the version
    /// a runtime recovery restored was deployed with it (none when it predates
    /// configuration management). Idempotent.
    pub fn restore(&self, name: &str, record: Option<&Record>) -> anyhow::Result<()> {
        if self.committed(name)?.as_ref() == record {
            return Ok(());
        }
        let dir = self.dir(name)?;
        let path = dir.join("committed.json");
        match record {
            Some(record) => {
                create_dir_durable(self.root.parent().unwrap_or(&self.root), &dir)?;
                write_private(&path, record)
            }
            None => {
                std::fs::remove_file(&path)?;
                sync_parent(&path)
            }
        }
    }

    pub fn discard(&self, name: &str) -> anyhow::Result<()> {
        match std::fs::remove_file(self.dir(name)?.join("staged.json")) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => Err(error.into()),
            _ => Ok(()),
        }
    }

    /// Resolve staged records left by an interrupted or finished install:
    /// commit, keep or discard each as `outcome` says.
    pub fn settle(&self, mut outcome: impl FnMut(&str, &str) -> Settled) -> anyhow::Result<()> {
        for entry in std::fs::read_dir(&self.root)?.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            if !crate::deps::valid_name(&name) {
                continue;
            }
            let Some(staged) = self.staged(&name)? else { continue };
            match outcome(&name, &staged.deployment) {
                Settled::Committed => self.commit(&name, &staged.deployment)?,
                Settled::Pending => {}
                Settled::Abandoned => self.discard(&name)?,
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn configuration(value: Value) -> Configuration {
        serde_json::from_value(value).unwrap()
    }
    fn object(value: Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn pointers_follow_rfc_6901() {
        assert!(tokens("").unwrap().is_empty());
        assert_eq!(tokens("/a~1b/c~0d/").unwrap(), ["a/b", "c~d", ""]);
        assert_eq!(tokens("/~01").unwrap(), ["~1"], "~0 is decoded after ~1");
        for bad in ["a", "/~", "/~2", "/a~"] {
            assert!(tokens(bad).is_err(), "{bad}");
        }
        assert!(tokens(&format!("/{}", "a".repeat(1024))).is_err());
    }

    #[test]
    fn first_install_takes_defaults_then_update() {
        let recipe = configuration(json!({
            "defaults": {"port": 8080, "log": {"level": "info", "files": ["a"]}},
            "merge": {"log": {"level": "debug"}, "extra": null}
        }));
        assert_eq!(
            Value::Object(next(None, &recipe).unwrap()),
            json!({"port": 8080, "log": {"level": "debug", "files": ["a"]}, "extra": null})
        );
    }

    #[test]
    fn existing_values_survive_new_defaults_until_reset() {
        let previous = object(json!({"port": 9000, "log": {"level": "debug"}, "old": true}));
        let upgrade = configuration(json!({"defaults": {"port": 8080, "log": {"level": "info", "format": "json"}}}));
        // Missing defaults are filled in; customized values stay.
        assert_eq!(
            Value::Object(next(Some(&previous), &upgrade).unwrap()),
            json!({"port": 9000, "log": {"level": "debug", "format": "json"}, "old": true})
        );
        // Reset takes the default where there is one and removes the key otherwise.
        let reset = configuration(json!({"defaults": {"port": 8080, "log": {"level": "info"}}, "reset": ["/port", "/old", "/missing/deeper"]}));
        assert_eq!(
            Value::Object(next(Some(&previous), &reset).unwrap()),
            json!({"port": 8080, "log": {"level": "debug"}})
        );
        // "" resets everything, before the merge is applied.
        let everything = configuration(json!({"defaults": {"port": 8080}, "reset": [""], "merge": {"port": 1}}));
        assert_eq!(Value::Object(next(Some(&previous), &everything).unwrap()), json!({"port": 1}));
    }

    #[test]
    fn merge_replaces_arrays_and_scalars_and_recurses_into_objects() {
        let mut target = object(json!({"a": [1, 2], "b": {"c": 1, "d": 2}, "e": {"f": 1}}));
        merge(&mut target, &object(json!({"a": [3], "b": {"c": 5}, "e": 7})));
        assert_eq!(Value::Object(target), json!({"a": [3], "b": {"c": 5, "d": 2}, "e": 7}));
    }

    #[test]
    fn pointers_never_address_array_elements_or_scalars() {
        let previous = object(json!({"list": [{"a": 1}], "n": 1}));
        for pointer in ["/list/0", "/list/0/a", "/n/x"] {
            let recipe = configuration(json!({"reset": [pointer]}));
            let error = next(Some(&previous), &recipe).unwrap_err().to_string();
            assert!(error.contains("non-object"), "{pointer}: {error}");
            assert!(!error.contains("\"a\": 1"), "errors name pointers, not values");
        }
    }

    #[test]
    fn recipes_cannot_carry_confidential_values() {
        let leaked = configuration(json!({"defaults": {"db": {"password": "hunter2"}}, "confidential": ["/db/password"]}));
        let error = validate_recipe(&leaked).unwrap_err().to_string();
        assert!(error.contains("/db/password") && !error.contains("hunter2"), "{error}");
        let merged = configuration(json!({"merge": {"db": {"password": "x"}}, "confidential": ["/db/password"]}));
        assert!(validate_recipe(&merged).is_err());
        let crossing = configuration(json!({"defaults": {"db": "x"}, "confidential": ["/db/password"]}));
        assert!(validate_recipe(&crossing).is_err(), "the overlay could never set it");
        assert!(validate_recipe(&configuration(json!({"confidential": [""]}))).is_err());
        // A value kept in public by an earlier version is dropped.
        let previous = object(json!({"db": {"password": "old", "host": "h"}}));
        let now = configuration(json!({"confidential": ["/db/password"]}));
        assert_eq!(Value::Object(next(Some(&previous), &now).unwrap()), json!({"db": {"host": "h"}}));
    }

    #[test]
    fn limits_bound_depth_and_size() {
        let mut deep = json!(1);
        for _ in 0..MAX_DEPTH {
            deep = json!({"a": deep});
        }
        assert!(validate_recipe(&configuration(json!({"defaults": deep}))).is_err());
        let wide: Map<String, Value> = (0..MAX_VALUES).map(|i| (i.to_string(), json!(i))).collect();
        assert!(validate_recipe(&configuration(json!({"defaults": wide}))).is_err());
        let fits: Map<String, Value> = (0..MAX_VALUES - 1).map(|i| (i.to_string(), json!(i))).collect();
        validate_recipe(&configuration(json!({"defaults": fits}))).unwrap();
        // Two documents that fit can still exceed the limit together.
        let half: Map<String, Value> = (0..MAX_VALUES / 2 + 1).map(|i| (format!("d{i}"), json!(i))).collect();
        let other: Map<String, Value> = (0..MAX_VALUES / 2 + 1).map(|i| (format!("m{i}"), json!(i))).collect();
        assert!(next(None, &configuration(json!({"defaults": half, "merge": other}))).is_err());
    }

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("suifleet-config-{name}-{}-{}", std::process::id(), rand::random::<u64>()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn overlay_supplies_confidential_values_privately() {
        let dir = temp("overlay");
        let record = Record {
            effective: object(json!({"db": {"host": "h"}})),
            confidential: vec!["/db/password".into()],
            ..Record::default()
        };
        assert!(overlay(&dir, "app").unwrap().is_empty(), "no overlay file");
        let error = materialize(&record, &Map::new()).unwrap_err().to_string();
        assert!(error.contains("not provisioned"), "{error}");
        let path = dir.join("app.json");
        std::fs::write(&path, r#"{"/db/password": "s3cret"}"#).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        let error = overlay(&dir, "app").unwrap_err().to_string();
        assert!(error.contains("other users") && !error.contains("s3cret"), "{error}");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o640)).unwrap();
        let values = overlay(&dir, "app").unwrap();
        assert_eq!(materialize(&record, &values).unwrap(), json!({"db": {"host": "h", "password": "s3cret"}}));
        std::fs::write(&path, "{\"/db/password\": s3cret").unwrap();
        let error = overlay(&dir, "app").unwrap_err().to_string();
        assert!(!error.contains("s3cret"), "{error}");
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn durable_directories_are_synced_up_to_their_base() {
        let dir = temp("durable");
        // `a` already exists, as a component directory created earlier by a
        // non-durable path would: its entry in `dir` is synced regardless.
        std::fs::create_dir_all(dir.join("a")).unwrap();
        let nested = dir.join("a/b/c");
        SYNCED.with(|synced| synced.borrow_mut().clear());
        create_dir_durable(&dir, &nested).unwrap();
        assert!(nested.is_dir());
        let synced = SYNCED.with(|synced| synced.take());
        assert_eq!(synced, [dir.clone(), dir.join("a"), dir.join("a/b")]);
        // Existing directories are accepted unchanged.
        create_dir_durable(&dir, &nested).unwrap();
        assert!(create_dir_durable(&dir, &dir).is_err());
        assert!(create_dir_durable(&nested, &dir).is_err());
    }

    #[test]
    fn staged_records_commit_or_discard_and_survive_reopening() {
        let dir = temp("store");
        let store = Store::open(&dir, Path::new("/nonexistent")).unwrap();
        let record = |deployment: &str, port| Record {
            deployment: deployment.into(),
            version: "1.0.0".into(),
            effective: object(json!({"port": port})),
            ..Record::default()
        };
        store.stage("app", &record("0x1", 1)).unwrap();
        assert!(store.committed("app").unwrap().is_none(), "staged is not committed");
        store.commit("app", "0x2").unwrap();
        assert!(store.committed("app").unwrap().is_none(), "only the staged deployment commits");
        store.commit("app", "0x1").unwrap();
        assert_eq!(store.committed("app").unwrap().unwrap(), record("0x1", 1));
        let mode = std::fs::metadata(dir.join("configuration/app/committed.json")).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        // Crash recovery, after reopening: each staged record is settled by its deployment.
        for (name, deployment) in [("lib", "0x2"), ("tool", "0x3"), ("fw", "0x4")] {
            store.stage(name, &record(deployment, 2)).unwrap();
        }
        let store = Store::open(&dir, Path::new("/nonexistent")).unwrap();
        store
            .settle(|_, deployment| match deployment {
                "0x2" => Settled::Committed,
                "0x3" => Settled::Abandoned,
                _ => Settled::Pending,
            })
            .unwrap();
        assert_eq!(store.committed("lib").unwrap().unwrap(), record("0x2", 2));
        assert!(store.committed("tool").unwrap().is_none() && store.staged("tool").unwrap().is_none());
        assert!(store.staged("fw").unwrap().is_some(), "a pending outcome keeps its record");
        assert!(store.stage("../x", &record("0x5", 1)).is_err());
        // A runtime recovery restores the recovered version's record, or none.
        store.restore("lib", Some(&record("0x1", 1))).unwrap();
        assert_eq!(store.committed("lib").unwrap().unwrap(), record("0x1", 1));
        store.restore("lib", Some(&record("0x1", 1))).unwrap();
        store.restore("lib", None).unwrap();
        assert!(store.committed("lib").unwrap().is_none());
        store.restore("lib", None).unwrap();
        store.restore("new", Some(&record("0x6", 3))).unwrap();
        assert_eq!(store.committed("new").unwrap().unwrap(), record("0x6", 3));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
