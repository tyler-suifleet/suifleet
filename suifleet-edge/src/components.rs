//! Coordinated multi-component deployments: loads authenticated dependency
//! candidates for the pure resolver in `deps`, and keeps the optional local
//! candidate store used by the `cached` offline policy.
//!
//! Candidates come only from the catalog the deployed version is bound to.
//! A cached version is a copy of a frozen on-chain object; cached recipes and
//! artifacts are content addressed and re-verified by SHA-256 before use. The
//! store is as trusted as the journal beside it.
use crate::{
    catalog::{self, CatalogRef},
    config::{ComponentsConfig, OfflinePolicy},
    deps,
    recipe::Recipe,
    state::Component,
    sui_client::{same_id, SuiClient},
    walrus_client::{sha256_file, WalrusClient},
};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::Write,
    path::{Path, PathBuf},
};
use tracing::warn;

/// Bound on catalog/recipe reads for one deployment's resolution.
const MAX_LOADS: usize = 256;

pub struct Store {
    root: PathBuf,
    max_bytes: u64,
}

fn digest(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}
/// Version strings are SemVer: path-safe once this holds.
fn version_text(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"-.+".contains(&b))
        && !s.starts_with('.')
}
fn catalog_dir(id: &str) -> Option<String> {
    let hex = id.strip_prefix("0x")?;
    (!hex.is_empty() && hex.len() <= 64 && hex.bytes().all(|b| b.is_ascii_hexdigit()))
        .then(|| format!("{:0>64}", hex.to_ascii_lowercase()))
}

fn write_atomic(path: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    std::fs::create_dir_all(path.parent().unwrap())?;
    let temporary = path.with_extension("tmp");
    let mut file = std::fs::File::create(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(temporary, path)?;
    Ok(())
}

/// Copy `source` to `dest`, leaving no partial file when the copy fails.
fn copy_or_remove(source: &Path, dest: &Path) -> anyhow::Result<()> {
    if let Err(error) = std::fs::copy(source, dest) {
        let _ = std::fs::remove_file(dest);
        return Err(error.into());
    }
    Ok(())
}

impl Store {
    /// The store exists only when the offline policy permits cached candidates.
    pub fn open(state_dir: &Path, config: &ComponentsConfig) -> Option<Self> {
        (config.offline_candidates == OfflinePolicy::Cached).then(|| Self {
            root: state_dir.join("components"),
            max_bytes: config.cache_max_bytes,
        })
    }

    fn version_path(&self, catalog: &str, name: &str, version: &str) -> Option<PathBuf> {
        (deps::valid_name(name) && version_text(version)).then_some(())?;
        Some(
            self.root
                .join("versions")
                .join(catalog_dir(catalog)?)
                .join(format!("{name}@{version}.json")),
        )
    }

    pub fn save_version(&self, id: &str, value: &catalog::Version) -> anyhow::Result<()> {
        let path = self
            .version_path(&value.catalog_id, &value.component_name, &value.version)
            .ok_or_else(|| anyhow::anyhow!("uncacheable component version"))?;
        let record = serde_json::json!({"version_id": id, "value": value});
        write_atomic(&path, &serde_json::to_vec_pretty(&record)?)
    }

    pub fn version(&self, catalog: &str, name: &str, version: &str) -> Option<(String, catalog::Version)> {
        let record: serde_json::Value =
            serde_json::from_slice(&std::fs::read(self.version_path(catalog, name, version)?).ok()?).ok()?;
        let value: catalog::Version = serde_json::from_value(record["value"].clone()).ok()?;
        let matches = same_id(&value.catalog_id, catalog)
            && value.component_name == name
            && value.version == version;
        Some((record["version_id"].as_str()?.to_owned(), value)).filter(|_| matches)
    }

    /// Cached `(version, version object ID)` entries of one component.
    pub fn versions(&self, catalog: &str, name: &str) -> Vec<(String, String)> {
        let Some(directory) = catalog_dir(catalog).map(|d| self.root.join("versions").join(d)) else {
            return Vec::new();
        };
        let mut result = Vec::new();
        for entry in std::fs::read_dir(directory).into_iter().flatten().flatten() {
            let file = entry.file_name().to_string_lossy().into_owned();
            let Some(version) = file
                .strip_suffix(".json")
                .and_then(|f| f.strip_prefix(name))
                .and_then(|f| f.strip_prefix('@'))
            else {
                continue;
            };
            if let Some((id, _)) = self.version(catalog, name, version) {
                result.push((version.to_owned(), id));
            }
        }
        result.sort();
        result
    }

    fn recipe(&self, sha256: &str) -> Option<Vec<u8>> {
        digest(sha256).then_some(())?;
        let bytes = std::fs::read(self.root.join("recipes").join(sha256)).ok()?;
        (hex::encode(Sha256::digest(&bytes)) == sha256).then_some(bytes)
    }

    fn save_recipe(&self, sha256: &str, bytes: &[u8]) -> anyhow::Result<()> {
        anyhow::ensure!(digest(sha256), "invalid recipe digest");
        write_atomic(&self.root.join("recipes").join(sha256), bytes)
    }

    /// Persist recency in mtimes across store reopen/restart. Advancing beyond
    /// every entry avoids ties for rapid accesses on coarse-resolution filesystems.
    /// These are logical ordering timestamps and may run ahead of wall time.
    fn touch_artifact(&self, path: &Path) -> anyhow::Result<()> {
        let mut latest = std::time::SystemTime::now();
        for entry in std::fs::read_dir(self.root.join("artifacts"))? {
            latest = latest.max(entry?.metadata()?.modified()?);
        }
        let next = latest
            .checked_add(std::time::Duration::from_secs(2))
            .ok_or_else(|| anyhow::anyhow!("cache recency timestamp overflow"))?;
        let file = std::fs::File::options().append(true).open(path)?;
        file.set_modified(next)?;
        anyhow::ensure!(
            file.metadata()?.modified()? > latest,
            "filesystem cannot preserve cache recency ordering"
        );
        Ok(())
    }

    /// Copy a cached artifact to `dest` if one with this digest verifies.
    /// The copy is held to the download size limit and free-space reserve.
    fn artifact(&self, walrus: &WalrusClient, sha256: &str, dest: &Path) -> anyhow::Result<bool> {
        let source = self.root.join("artifacts").join(sha256);
        if !digest(sha256) || !source.exists() {
            return Ok(false);
        }
        let size = std::fs::metadata(&source)?.len();
        walrus.ensure_room(dest.parent().unwrap_or(Path::new(".")), size)?;
        copy_or_remove(&source, dest)?;
        if sha256_file(dest)? != sha256 {
            warn!(sha256, "Discarding corrupt cached artifact");
            let _ = std::fs::remove_file(dest);
            let _ = std::fs::remove_file(source);
            return Ok(false);
        }
        // Recently used entries are evicted last.
        self.touch_artifact(&source)?;
        Ok(true)
    }

    /// Keep a verified artifact, refused when the copy would eat into the
    /// free-space reserve.
    fn save_artifact(&self, walrus: &WalrusClient, sha256: &str, path: &Path) -> anyhow::Result<()> {
        anyhow::ensure!(digest(sha256), "invalid artifact digest");
        let target = self.root.join("artifacts").join(sha256);
        if target.exists() {
            return Ok(());
        }
        std::fs::create_dir_all(target.parent().unwrap())?;
        walrus.ensure_room(target.parent().unwrap(), std::fs::metadata(path)?.len())?;
        let temporary = target.with_extension("tmp");
        copy_or_remove(path, &temporary)?;
        std::fs::File::open(&temporary)?.sync_all()?;
        std::fs::rename(temporary, &target)?;
        self.touch_artifact(&target)
    }

    /// Evict cached artifacts oldest first beyond `max_bytes`, never those in `keep`.
    pub fn evict(&self, keep: &BTreeSet<String>) -> anyhow::Result<u64> {
        let mut entries = Vec::new();
        for entry in std::fs::read_dir(self.root.join("artifacts")).into_iter().flatten().flatten() {
            let metadata = entry.metadata()?;
            entries.push((metadata.modified()?, metadata.len(), entry.path()));
        }
        let mut total: u64 = entries.iter().map(|(_, size, _)| size).sum();
        entries.sort();
        let mut freed = 0;
        for (_, size, path) in entries {
            if total <= self.max_bytes {
                break;
            }
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            if keep.contains(&name) {
                continue;
            }
            std::fs::remove_file(&path)?;
            total -= size;
            freed += size;
        }
        Ok(freed)
    }
}

/// An authenticated, parsed recipe, and whether it was Seal-encrypted.
pub struct Authentic {
    pub recipe: Recipe,
    pub confidential: bool,
}

impl Authentic {
    /// Check the recipe; a confidential recipe's error names only `stage`.
    pub fn check<T>(&self, stage: &str, check: impl FnOnce(&Recipe) -> anyhow::Result<T>) -> anyhow::Result<T> {
        withhold(self.confidential, stage, check(&self.recipe))
    }
}

/// Parse and validation errors quote recipe values (commands, configuration,
/// dependency requirements). For confidential content keep only the stage,
/// so its plaintext never reaches logs, the journal or telemetry.
pub fn withhold<T>(confidential: bool, stage: &str, result: anyhow::Result<T>) -> anyhow::Result<T> {
    result.map_err(|error| if confidential { withheld(stage) } else { error })
}

fn withheld(stage: &str) -> anyhow::Error {
    anyhow::anyhow!("confidential recipe rejected at {stage} (details withheld)")
}

/// Authenticated recipe: a verified cached copy when permitted, otherwise
/// Walrus. A Seal-encrypted recipe is decrypted and parsed in memory and never
/// cached; with `[seal] mode = "required"` an unencrypted one is refused and
/// the (plaintext-only) cache is not consulted.
pub async fn recipe(
    walrus: &WalrusClient,
    store: Option<&Store>,
    blob_id: &str,
    sha256: &str,
) -> anyhow::Result<Authentic> {
    let plain = |bytes: &[u8]| {
        Ok(Authentic {
            recipe: serde_json::from_slice(bytes)?,
            confidential: false,
        })
    };
    let store = store.filter(|_| !walrus.confidential_only());
    if let Some(bytes) = store.and_then(|s| s.recipe(sha256)) {
        return plain(&bytes);
    }
    tokio::time::timeout(walrus.deadline(), async {
        let mut error = anyhow::anyhow!("no Walrus mirrors configured");
        for mirror in walrus.mirrors() {
            match tokio::time::timeout(
                walrus.mirror_deadline(),
                recipe_from_mirror(&mirror, store, blob_id, sha256),
            )
            .await
            {
                Ok(Ok(recipe)) => return Ok(recipe),
                Ok(Err(e)) => error = e,
                Err(_) => error = anyhow::anyhow!("recipe mirror deadline exceeded"),
            }
        }
        Err(error)
    })
    .await
    .map_err(|_| anyhow::anyhow!("recipe mirror deadline exceeded"))?
}

async fn recipe_from_mirror(
    walrus: &WalrusClient,
    store: Option<&Store>,
    blob_id: &str,
    sha256: &str,
) -> anyhow::Result<Authentic> {
    let bytes = walrus.recipe_bytes(blob_id).await?;
    if crate::seal::is_envelope(&bytes) {
        let plaintext = walrus
            .open(bytes, crate::seal::Kind::Recipe, sha256)
            .await?;
        let recipe = serde_json::from_slice(&plaintext).map_err(|error| {
            // Its category and position are content-free; its message is not.
            withheld(&format!(
                "parsing ({:?} error at line {} column {})",
                error.classify(),
                error.line(),
                error.column()
            ))
        })?;
        return Ok(Authentic {
            recipe,
            confidential: true,
        });
    }
    anyhow::ensure!(
        !walrus.confidential_only(),
        "unencrypted recipe refused: [seal] mode = \"required\" accepts only confidential delivery"
    );
    anyhow::ensure!(
        hex::encode(Sha256::digest(&bytes)) == sha256,
        "recipe SHA-256 mismatch"
    );
    if let Some(store) = store {
        if let Err(error) = store.save_recipe(sha256, &bytes) {
            warn!(%error, "Cannot cache recipe");
        }
    }
    Ok(Authentic {
        recipe: serde_json::from_slice(&bytes)?,
        confidential: false,
    })
}

/// Write decrypted plaintext beside `dest` (owner-only) and move it into place.
fn replace_private(dest: &Path, bytes: &[u8]) -> anyhow::Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    let mut name = dest.file_name().unwrap_or_default().to_os_string();
    name.push(".plain");
    let staging = dest.with_file_name(name);
    let _ = std::fs::remove_file(&staging);
    let written = (|| -> anyhow::Result<()> {
        let mut file = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&staging)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::rename(&staging, dest)?;
        Ok(())
    })();
    if written.is_err() {
        let _ = std::fs::remove_file(&staging);
    }
    written
}

/// A verified artifact at `dest`: a cached copy when permitted, otherwise Walrus.
/// A Seal-encrypted artifact is decrypted in memory, checked against the
/// on-chain digest and only then written (owner-only); it is never cached.
pub async fn artifact(
    walrus: &WalrusClient,
    store: Option<&Store>,
    blob_id: &str,
    sha256: &str,
    dest: &Path,
) -> anyhow::Result<()> {
    let store = store.filter(|_| !walrus.confidential_only());
    if let Some(store) = store {
        if store.artifact(walrus, sha256, dest)? {
            return Ok(());
        }
    }
    tokio::time::timeout(walrus.deadline(), async {
        let mut error = anyhow::anyhow!("no Walrus mirrors configured");
        for (index, mirror) in walrus.mirrors().enumerate() {
            // Start each fallback mirror cleanly. The first attempt can resume
            // persisted bytes; the final digest authenticates the entire file.
            if index > 0 {
                WalrusClient::discard_partial(dest)?;
            }
            match tokio::time::timeout(
                walrus.mirror_deadline(),
                artifact_from_mirror(&mirror, blob_id, sha256, dest),
            )
            .await
            {
                Ok(Ok(())) => return Ok(()),
                outcome => {
                    let e = match outcome {
                        Ok(Err(e)) => e,
                        _ => anyhow::anyhow!("artifact mirror deadline exceeded"),
                    };
                    let _ = std::fs::remove_file(dest);
                    error = e;
                }
            }
        }
        // A complete corrupt candidate must not poison the next poll's resume.
        Err(error)
    })
    .await
    .map_err(|_| anyhow::anyhow!("artifact mirror deadline exceeded"))??;
    if let Some(store) = store {
        if let Err(error) = store.save_artifact(walrus, sha256, dest) {
            warn!(%error, "Cannot cache artifact");
        }
    }
    Ok(())
}

async fn artifact_from_mirror(
    walrus: &WalrusClient,
    blob_id: &str,
    sha256: &str,
    dest: &Path,
) -> anyhow::Result<()> {
    let actual = walrus.download(blob_id, dest).await?;
    if actual != sha256 && is_envelope_file(dest)? {
        let ciphertext = std::fs::read(dest)?;
        let _ = std::fs::remove_file(dest);
        let plaintext = walrus
            .open(ciphertext, crate::seal::Kind::Artifact, sha256)
            .await?;
        return replace_private(dest, &plaintext);
    }
    anyhow::ensure!(
        actual == sha256,
        "SHA-256 mismatch: expected {} got {}",
        sha256,
        actual
    );
    if walrus.confidential_only() {
        let _ = std::fs::remove_file(dest);
        anyhow::bail!("unencrypted artifact refused: [seal] mode = \"required\" accepts only confidential delivery");
    }
    Ok(())
}

fn is_envelope_file(path: &Path) -> anyhow::Result<bool> {
    use std::io::Read;
    let mut head = vec![0u8; crate::seal::MAGIC.len()];
    let mut file = std::fs::File::open(path)?;
    Ok(file.read_exact(&mut head).is_ok() && crate::seal::is_envelope(&head))
}

/// An authenticated dependency candidate selected for this platform.
#[derive(Debug, Clone)]
pub struct Loaded {
    pub version_id: String,
    pub recipe_sha256: String,
    pub artifact_sha256: String,
    pub artifact_blob_id: String,
    pub install: Option<String>,
    /// Present for a supervised (run) dependency.
    pub supervised: Option<crate::supervisor::Lifecycle>,
    pub configuration: crate::protocol::Configuration,
    pub validate: Option<String>,
    /// Its recipe was Seal-encrypted (see `withhold`).
    pub confidential: bool,
}

pub struct Loader<'a> {
    sui: &'a SuiClient,
    walrus: &'a WalrusClient,
    store: Option<&'a Store>,
    catalog: Option<&'a CatalogRef>,
    allow_commands: bool,
    index: Option<Vec<(String, String, String)>>,
    offline: bool,
    ids: BTreeMap<(String, String), String>,
    pub loaded: BTreeMap<(String, String), Loaded>,
    /// A confidential recipe was loaded: resolution errors may quote it.
    pub confidential: bool,
}

impl<'a> Loader<'a> {
    pub fn new(
        sui: &'a SuiClient,
        walrus: &'a WalrusClient,
        store: Option<&'a Store>,
        catalog: Option<&'a CatalogRef>,
        allow_commands: bool,
    ) -> Self {
        Self {
            sui,
            walrus,
            store,
            catalog,
            allow_commands,
            index: None,
            offline: false,
            ids: BTreeMap::new(),
            loaded: BTreeMap::new(),
            confidential: false,
        }
    }

    /// Versions of `name` in the deployment's catalog. Schema-1 deployments
    /// have no catalog: only installed components can satisfy them.
    async fn listing(&mut self, name: &str) -> anyhow::Result<Vec<deps::Candidate>> {
        let Some(catalog) = self.catalog else {
            return Ok(Vec::new());
        };
        if self.index.is_none() && !self.offline {
            match catalog::index(self.sui, catalog).await {
                Ok(index) => self.index = Some(index),
                Err(error) if self.store.is_some() => {
                    warn!(%error, "Component catalog unreachable; resolving from cached candidates");
                    self.offline = true;
                }
                Err(error) => return Err(error.context("component catalog unavailable")),
            }
        }
        let entries = match &self.index {
            Some(index) => index
                .iter()
                .filter(|(n, _, _)| n == name)
                .map(|(_, version, id)| (version.clone(), id.clone()))
                .collect(),
            None => self.store.unwrap().versions(&catalog.catalog_id, name),
        };
        Ok(entries
            .into_iter()
            .map(|(version, id)| {
                self.ids.insert((name.to_owned(), version.clone()), id);
                deps::Candidate {
                    version,
                    detail: None,
                }
            })
            .collect())
    }

    async fn detail(&mut self, name: &str, version: &str) -> anyhow::Result<deps::Detail> {
        let catalog = self.catalog.expect("listed candidates have a catalog");
        let id = self.ids[&(name.to_owned(), version.to_owned())].clone();
        let cached = self
            .store
            .and_then(|s| s.version(&catalog.catalog_id, name, version))
            .filter(|(cached_id, _)| same_id(cached_id, &id));
        let value = match cached {
            // A frozen object: the cached copy is the on-chain value.
            Some((_, value)) => value,
            None => {
                let value = catalog::version(self.sui, catalog, &id, name, version).await?;
                if let Some(store) = self.store {
                    if let Err(error) = store.save_version(&id, &value) {
                        warn!(%error, "Cannot cache component version");
                    }
                }
                value
            }
        };
        value.validate()?;
        let (os, arch) = (std::env::consts::OS, std::env::consts::ARCH);
        let Some(i) = value.platform(os, arch) else {
            return Ok(deps::Detail::Unavailable(format!("no {os}/{arch} manifest")));
        };
        let authentic = recipe(self.walrus, self.store, &value.recipe_blob_ids[i], &value.recipe_sha256s[i]).await?;
        self.confidential |= authentic.confidential;
        let (dependencies, supervised) = authentic.check("validation", |recipe| {
            anyhow::ensure!(recipe.services.is_none(), "dependency recipes cannot own deployment services; declare them in the authenticated root recipe");
            let dependencies = recipe.validate_component(name, version, &value.artifact_sha256s[i], self.allow_commands)?;
            Ok((dependencies, crate::supervisor::Lifecycle::of(&recipe.lifecycle)?))
        })?;
        let Authentic { recipe: parsed, confidential } = authentic;
        self.loaded.insert(
            (name.to_owned(), version.to_owned()),
            Loaded {
                version_id: id,
                recipe_sha256: value.recipe_sha256s[i].clone(),
                artifact_sha256: value.artifact_sha256s[i].clone(),
                supervised,
                artifact_blob_id: parsed.artifact_blob_id,
                install: parsed.lifecycle.install,
                configuration: parsed.configuration,
                validate: parsed.lifecycle.validate,
                confidential,
            },
        );
        Ok(deps::Detail::Ready(dependencies))
    }
}

/// The installed components as the resolver sees them.
pub fn installed(components: &BTreeMap<String, Component>) -> BTreeMap<String, deps::Installed> {
    components
        .iter()
        .map(|(name, c)| {
            (
                name.clone(),
                deps::Installed {
                    version: c.version.clone(),
                    dependencies: c.dependencies.clone(),
                    confirmed: c.confirmed,
                },
            )
        })
        .collect()
}

/// Resolve completely, loading only what the resolver asks for. Nothing on
/// the device changes here; every rejection happens before an install.
pub async fn plan(
    loader: &mut Loader<'_>,
    root: &deps::Root,
    installed: &BTreeMap<String, deps::Installed>,
) -> anyhow::Result<deps::Plan> {
    let mut pool = deps::Pool::default();
    for _ in 0..MAX_LOADS {
        match deps::resolve(root, installed, &pool)? {
            deps::Step::Plan(plan) => return Ok(plan),
            deps::Step::Need(deps::Need::Listing(name)) => {
                let listing = loader.listing(&name).await?;
                pool.listings.insert(name, listing);
            }
            deps::Step::Need(deps::Need::Detail { name, version }) => {
                let detail = loader
                    .detail(&name, &version)
                    .await
                    .map_err(|e| e.context(format!("dependency candidate {name} {version}")))?;
                pool.candidate_mut(&name, &version).unwrap().detail = Some(detail);
            }
        }
    }
    anyhow::bail!("dependency resolution needed more than {MAX_LOADS} catalog reads")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store(name: &str, max_bytes: u64) -> (PathBuf, Store) {
        let dir = std::env::temp_dir().join(format!("suifleet-store-{name}-{}-{}", std::process::id(), rand::random::<u64>()));
        let config = ComponentsConfig { offline_candidates: OfflinePolicy::Cached, cache_max_bytes: max_bytes, ..ComponentsConfig::default() };
        assert!(Store::open(&dir, &ComponentsConfig::default()).is_none(), "never policy keeps no store");
        (dir.clone(), Store::open(&dir, &config).unwrap())
    }
    fn limits(max_artifact_bytes: u64, min_free_bytes: u64) -> WalrusClient {
        WalrusClient::new(&crate::config::WalrusConfig { max_artifact_bytes, min_free_bytes, ..Default::default() })
    }
    fn version(name: &str, version: &str) -> catalog::Version {
        serde_json::from_value(serde_json::json!({
            "catalog_id": "0xCAFE", "publisher": "0x1", "component_name": name, "version": version,
            "schema_version": "1", "platform_os": ["linux"], "platform_arch": ["*"],
            "recipe_blob_ids": ["r"], "recipe_sha256s": ["a".repeat(64)], "artifact_sha256s": ["b".repeat(64)]
        }))
        .unwrap()
    }

    #[test]
    fn confidential_recipe_errors_withhold_their_content() {
        let value = serde_json::json!({"schemaVersion":"1.1","componentName":"app","componentVersion":"1",
            "artifactBlobId":"blob","artifactSha256":"a".repeat(64),"lifecycle":{},"configuration":{},
            "componentDependencies":{"helper":{"versionRequirement":"SECRET-requirement"}}});
        let validate = |recipe: &Recipe| recipe.validate_component("app", "1", &"a".repeat(64), false);
        let plain = Authentic { recipe: serde_json::from_value(value.clone()).unwrap(), confidential: false };
        assert!(format!("{:#}", plain.check("validation", validate).unwrap_err()).contains("SECRET-requirement"));
        let sealed = Authentic { recipe: serde_json::from_value(value).unwrap(), confidential: true };
        let error = format!("{:#}", sealed.check("validation", validate).unwrap_err());
        assert_eq!(error, "confidential recipe rejected at validation (details withheld)");
        assert_eq!(withhold(true, "configuration merge", Ok(7)).unwrap(), 7);
        let failed: anyhow::Result<()> = Err(anyhow::anyhow!("pointer \"/SECRET\""));
        assert!(!format!("{:#}", withhold(true, "configuration merge", failed).unwrap_err()).contains("SECRET"));
    }

    #[test]
    fn cached_versions_are_listed_by_catalog_and_name() {
        let (dir, store) = store("versions", 0);
        store.save_version("0x10", &version("lib", "1.0.0")).unwrap();
        store.save_version("0x11", &version("lib", "1.1.0-rc.1+b")).unwrap();
        store.save_version("0x12", &version("lib-extra", "1.0.0")).unwrap();
        assert_eq!(
            store.versions("0xcafe", "lib"),
            [("1.0.0".to_string(), "0x10".to_string()), ("1.1.0-rc.1+b".into(), "0x11".into())]
        );
        assert!(store.versions("0xbeef", "lib").is_empty());
        assert!(store.version("0xcafe", "../lib", "1.0.0").is_none());
        let mut forged = version("lib", "1.0.0");
        forged.component_name = "../../x".into();
        assert!(store.save_version("0x13", &forged).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn cached_content_is_verified_and_evicted_oldest_first() {
        let (dir, store) = store("content", 10);
        let bytes = b"{\"recipe\":1}";
        let sha = hex::encode(Sha256::digest(bytes));
        store.save_recipe(&sha, bytes).unwrap();
        assert_eq!(store.recipe(&sha).unwrap(), bytes);
        std::fs::write(store.root.join("recipes").join(&sha), b"tampered").unwrap();
        assert!(store.recipe(&sha).is_none(), "a tampered recipe is never used");

        let work = dir.join("work");
        std::fs::create_dir_all(&work).unwrap();
        let walrus = limits(1 << 20, 0);
        let mut digests = Vec::new();
        for (i, content) in [b"12345".as_slice(), b"abcde", b"vwxyz"].iter().enumerate() {
            let path = work.join(format!("a{i}"));
            std::fs::write(&path, content).unwrap();
            let sha = sha256_file(&path).unwrap();
            store.save_artifact(&walrus, &sha, &path).unwrap();
            // Whole-second, widely spaced mtimes: no scheduling or sub-second
            // filesystem resolution assumption. The cache hit below must move
            // the first entry after even these future timestamps.
            let modified = std::time::UNIX_EPOCH
                + std::time::Duration::from_secs(2_000_000_000 + i as u64 * 60);
            std::fs::File::options()
                .append(true)
                .open(store.root.join("artifacts").join(&sha))
                .unwrap()
                .set_modified(modified)
                .unwrap();
            digests.push(sha);
        }
        let out = work.join("out");
        assert!(store.artifact(&walrus, &digests[0], &out).unwrap());
        assert_eq!(std::fs::read(&out).unwrap(), b"12345");
        // 15 bytes > 10: the oldest unprotected entry goes; a kept one stays.
        let keep = BTreeSet::from([digests[1].clone()]);
        assert_eq!(store.evict(&keep).unwrap(), 5);
        assert!(!store.artifact(&walrus, &digests[2], &out).unwrap());
        assert!(store.artifact(&walrus, &digests[1], &out).unwrap() && store.artifact(&walrus, &digests[0], &out).unwrap());
        std::fs::write(store.root.join("artifacts").join(&digests[0]), b"corrupt").unwrap();
        assert!(!store.artifact(&walrus, &digests[0], &out).unwrap(), "corrupt cache entries are discarded");
        assert!(!store.root.join("artifacts").join(&digests[0]).exists());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn artifact_recency_survives_rapid_writes_and_store_reopen() {
        let (dir, store) = store("recency", 5);
        let walrus = limits(1 << 20, 0);
        let input = dir.join("input");
        std::fs::create_dir_all(&dir).unwrap();
        let mut entries = Vec::new();
        for content in [b"12345", b"abcde", b"vwxyz"] {
            std::fs::write(&input, content).unwrap();
            let sha = sha256_file(&input).unwrap();
            store.save_artifact(&walrus, &sha, &input).unwrap();
            let path = store.root.join("artifacts").join(sha);
            let modified = std::fs::metadata(&path).unwrap().modified().unwrap();
            if let Some((_, previous)) = entries.last() {
                assert!(modified > *previous, "rapid writes have distinct recency");
            }
            entries.push((path, modified));
        }
        // Reopen: recency must not rely on an in-memory counter.
        let reopened = Store::open(
            &dir,
            &ComponentsConfig {
                offline_candidates: OfflinePolicy::Cached,
                cache_max_bytes: 5,
                ..ComponentsConfig::default()
            },
        )
        .unwrap();
        let sha = entries[0].0.file_name().unwrap().to_str().unwrap();
        assert!(reopened.artifact(&walrus, sha, &input).unwrap());
        assert!(
            std::fs::metadata(&entries[0].0)
                .unwrap()
                .modified()
                .unwrap()
                > entries[2].1
        );
        assert_eq!(reopened.evict(&BTreeSet::new()).unwrap(), 10);
        assert!(entries[0].0.exists());
        assert!(!entries[1].0.exists() && !entries[2].0.exists());
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn cached_artifacts_respect_the_download_limits() {
        let (dir, store) = store("limits", 1 << 20);
        let work = dir.join("work");
        std::fs::create_dir_all(&work).unwrap();
        let path = work.join("a");
        std::fs::write(&path, b"12345").unwrap();
        let sha = sha256_file(&path).unwrap();
        let cached = store.root.join("artifacts").join(&sha);

        // Populating the cache never eats into the free-space reserve.
        let error = store.save_artifact(&limits(1 << 20, u64::MAX / 2), &sha, &path).unwrap_err();
        assert!(error.to_string().contains("insufficient disk space"), "{error}");
        assert!(!cached.exists() && !cached.with_extension("tmp").exists());
        store.save_artifact(&limits(1 << 20, 0), &sha, &path).unwrap();

        // A warm cache is refused under a tightened size limit or reserve.
        let out = work.join("out");
        let error = store.artifact(&limits(4, 0), &sha, &out).unwrap_err();
        assert!(error.to_string().contains("download limit"), "{error}");
        let error = store.artifact(&limits(1 << 20, u64::MAX / 2), &sha, &out).unwrap_err();
        assert!(error.to_string().contains("insufficient disk space"), "{error}");
        assert!(!out.exists() && cached.exists(), "a refusal writes nothing and keeps the entry");
        assert!(store.artifact(&limits(5, 0), &sha, &out).unwrap());
        std::fs::remove_dir_all(dir).unwrap();
    }
}
