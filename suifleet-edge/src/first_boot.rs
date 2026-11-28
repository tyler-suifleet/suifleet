use crate::{
    config::{Config, DeviceConfig, EnrollConfig, ManagedEnrollment, SuiConfig},
    identity,
    sui_client::SuiClient,
};
use anyhow::{Context, Result};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    os::{
        fd::AsRawFd,
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::{Path, PathBuf},
    time::Duration,
};
use sui_sdk_types::Address;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Trust {
    policy_id: Address,
    operator: Address,
    manifest_sha256: String,
    template_sha256: Option<String>,
    history_start_checkpoint: u64,
    #[serde(default = "default_locations")]
    locations: Vec<PathBuf>,
    managed: Option<ManagedEnrollment>,
}

fn default_locations() -> Vec<PathBuf> {
    ["/boot/enrollment", "/boot/firmware/enrollment"]
        .into_iter()
        .map(PathBuf::from)
        .collect()
}

fn open_regular(path: &Path, write: bool, limit: u64) -> Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(write)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let info = file.metadata()?;
    // FAT reports a synthetic single hard link. Refuse aliases on other media.
    anyhow::ensure!(
        info.is_file() && info.nlink() == 1 && info.len() <= limit,
        "invalid provisioning file"
    );
    Ok(file)
}

fn read(file: &File, limit: u64) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    let mut reader = file;
    reader.seek(SeekFrom::Start(0))?;
    reader.take(limit + 1).read_to_end(&mut bytes)?;
    anyhow::ensure!(
        bytes.len() as u64 <= limit,
        "provisioning file exceeds size limit"
    );
    Ok(bytes)
}

fn check_digest(bytes: &[u8], digest: &str) -> Result<()> {
    anyhow::ensure!(
        hex::encode(Sha256::digest(bytes)) == digest,
        "provisioning content digest mismatch"
    );
    Ok(())
}

fn open_directory(path: &Path) -> std::io::Result<File> {
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(path)
}

// Keep media accesses anchored even if a directory entry is replaced after
// discovery. No credential, trust or status path follows a leaf symlink.
fn directory_path(directory: &File) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{}", directory.as_raw_fd()))
}

fn discover(paths: &[PathBuf]) -> Result<Vec<File>> {
    let mut folders = Vec::new();
    for location in paths {
        let directory = match open_directory(location) {
            Ok(directory) => directory,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        for entry in fs::read_dir(directory_path(&directory))? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                let folder = open_directory(&entry.path())?;
                if directory_path(&folder)
                    .join("enroll.toml")
                    .symlink_metadata()
                    .is_ok()
                    && !folders
                        .iter()
                        .any(|known: &File| same_file(known, &folder).unwrap_or(false))
                {
                    folders.push(folder);
                }
            }
        }
    }
    Ok(folders)
}

fn same_file(left: &File, right: &File) -> std::io::Result<bool> {
    let left = left.metadata()?;
    let right = right.metadata()?;
    Ok(left.dev() == right.dev() && left.ino() == right.ino())
}

fn status(folders: &[File], code: &str) {
    let message = format!("{{\"enrollment\":\"{code}\"}}");
    println!("{message}");
    for directory in folders {
        if write_status(directory, &message).is_err() {
            eprintln!("{{\"enrollment\":\"media-status-unavailable\"}}");
        }
    }
}

fn write_status(directory: &File, message: &str) -> Result<()> {
    let folder = directory_path(directory);
    let temporary = folder.join(format!(".enrollment-status-{:016x}", rand::random::<u64>()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    let result = (|| -> Result<()> {
        writeln!(file, "{message}")?;
        file.sync_all()?;
        fs::rename(&temporary, folder.join("enrollment-status.json"))?;
        directory.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        // Never remove an existing status file on a write failure.
        let _ = fs::remove_file(temporary);
    }
    result
}

#[derive(Debug)]
struct Failure(&'static str);

impl std::fmt::Display for Failure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}

impl std::error::Error for Failure {}

pub async fn run(root: &Path) -> Result<()> {
    let result = run_inner(root).await;
    // RPC and parser diagnostics may contain endpoints or user-supplied text.
    if let Err(error) = result {
        let code = error
            .downcast_ref::<Failure>()
            .map_or("local-configuration-error", |failure| failure.0);
        status(&[], code);
        return Err(Failure(code).into());
    }
    Ok(())
}

async fn run_inner(root: &Path) -> Result<()> {
    let root_dir = File::open(root)?;
    let root_metadata = root_dir.metadata()?;
    anyhow::ensure!(
        root_metadata.is_dir() && root_metadata.uid() == 0 && root_metadata.mode() & 0o022 == 0,
        "enrollment directory must be controlled by root"
    );
    // SAFETY: lock is associated with this directory descriptor, held until exit.
    if unsafe { libc::flock(root_dir.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(Failure("enrollment-already-running").into());
    }
    if root.join("config.toml").symlink_metadata().is_ok() {
        return Ok(());
    }
    let trust = open_regular(&root.join("first-boot.toml"), false, 16384)?;
    let metadata = trust.metadata()?;
    anyhow::ensure!(
        metadata.uid() == 0 && metadata.mode() & 0o022 == 0,
        "enrollment trust must be controlled by root"
    );
    let trust: Trust = toml::from_str(std::str::from_utf8(&read(&trust, 16384)?)?)?;
    let folders = discover(&trust.locations)?;
    if folders.len() != 1 {
        status(
            &folders,
            if folders.is_empty() {
                "media-not-found"
            } else {
                "ambiguous-media"
            },
        );
        return Err(Failure("expected-one-credential-folder").into());
    }
    let result = enroll(root, &folders[0], &trust).await;
    match result {
        Ok(()) => status(&folders, "enrolled"),
        Err(ref error) => status(
            &folders,
            error
                .downcast_ref::<Failure>()
                .map_or("invalid-enrollment-media-or-config", |f| f.0),
        ),
    }
    result
}

async fn enroll(root: &Path, directory: &File, trust: &Trust) -> Result<()> {
    let folder = directory_path(directory);
    let enroll_file = open_regular(&folder.join("enroll.toml"), false, 16384)?;
    let enroll: EnrollConfig = toml::from_str(std::str::from_utf8(&read(&enroll_file, 16384)?)?)?;
    anyhow::ensure!(
        enroll.policy_id.parse::<Address>()? == trust.policy_id
            && enroll.manifest_path == Path::new("deployment.json")
            && enroll.claim_key_path == Path::new("claim.key")
            && enroll.signer.is_none()
            && enroll.managed == trust.managed
            && valid_name(&enroll.name),
        "invalid enrollment metadata"
    );
    let manifest = read(
        &open_regular(&folder.join("deployment.json"), false, 262144)?,
        262144,
    )?;
    check_digest(&manifest, &trust.manifest_sha256)?;
    let manifest_parsed: crate::config::DeploymentManifest = serde_json::from_slice(&manifest)?;
    manifest_parsed.validate()?;
    let raw_template = match &trust.template_sha256 {
        Some(hash) => {
            let template = read(
                &open_regular(&folder.join("config.template.toml"), false, 262144)?,
                262144,
            )?;
            check_digest(&template, hash)?;
            template
        }
        None => {
            anyhow::ensure!(
                folder
                    .join("config.template.toml")
                    .symlink_metadata()
                    .is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound),
                "unexpected provisioning template"
            );
            b"[apply]\ncommand = \"/usr/bin/false\"\n".to_vec()
        }
    };
    let mut template: toml::Table = toml::from_str(std::str::from_utf8(&raw_template)?)?;
    anyhow::ensure!(
        !template.contains_key("sui") && !template.contains_key("device"),
        "template cannot override device or deployment identity"
    );
    template.insert(
        "sui".into(),
        toml::Value::Table(toml::from_str("manifest_path = \"deployment.json\"")?),
    );
    let mut device: toml::Table = toml::from_str(
        r#"
device_object_id = "0x0"
device_cap_id = "0x0"
[signer]
kind = "service"
socket = "/run/suifleet-signer/sign.sock"
"#,
    )?;
    template.insert("device".into(), toml::Value::Table(device.clone()));
    let daemon_gid = group_id("suifleet")?;
    let signer_gid = group_id("suifleet-signer")?;
    // Preview in the destination directory to exercise ordinary config validation
    // (including relative paths) before sending a transaction.
    let preview = Config::load_text(
        &root.join("config.toml"),
        &toml::to_string(&template)?,
        Some(manifest_parsed.clone()),
    )?;
    let mut signer = toml::Table::new();
    signer.insert("manifest_path".into(), "deployment.json".into());
    signer.insert("caller_unit".into(), "suifleet.service".into());
    if let Some(data) = template.get("fleet_data") {
        signer.insert("fleet_data".into(), data.clone());
    }
    if let Some(budget) = &preview.budget {
        let mut draws = toml::Table::new();
        draws.insert("budget_id".into(), budget.budget_id.clone().into());
        draws.insert(
            "max_draw_mist".into(),
            i64::try_from(budget.draw_mist)?.into(),
        );
        signer.insert("budget".into(), toml::Value::Table(draws));
    }
    if let Some(seal) = &preview.seal {
        let mut sessions = toml::Table::new();
        sessions.insert(
            "session_ttl_minutes".into(),
            i64::from(seal.session_ttl_minutes).into(),
        );
        sessions.insert(
            "clock_skew_seconds".into(),
            i64::try_from(seal.clock_skew_seconds)?.into(),
        );
        signer.insert("seal".into(), toml::Value::Table(sessions));
    }
    check_existing(root, "deployment.json", &manifest, daemon_gid)?;

    let keypair_path = root.join("device.key");
    let device_config = DeviceConfig {
        keypair_path: keypair_path.clone(),
        device_object_id: String::new(),
        device_cap_id: String::new(),
        signer: None,
    };
    let sui = SuiClient::load(
        SuiConfig {
            manifest_path: root.join("deployment.json"),
            rpc_url: manifest_parsed.rpc_url.clone(),
            rpc_urls: manifest_parsed.rpc_urls.clone(),
            package_id: manifest_parsed.call_package_id.clone(),
            manifest: Some(manifest_parsed),
        },
        identity::signer(&device_config)?,
    )?;
    let policy_id = trust.policy_id.to_string();
    let operator = trust.operator.to_string();
    let claim_path = folder.join("claim.key");
    let claim_file = match open_regular(&claim_path, true, 128) {
        Ok(file) => Some(file),
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
        {
            None
        }
        Err(error) => return Err(error),
    };
    let parsed_claim = claim_file
        .as_ref()
        .map(|file| -> Result<Option<identity::Ed25519File>> {
            let bytes = read(file, 128)?;
            if !bytes.is_empty() && bytes.iter().all(|byte| *byte == 0) {
                return Ok(None);
            }
            let seed = hex::decode(std::str::from_utf8(&bytes)?.trim())
                .context(Failure("invalid-claim-key"))?;
            let seed = seed.try_into().map_err(|_| Failure("invalid-claim-key"))?;
            Ok(Some(identity::Ed25519File(
                ed25519_dalek::SigningKey::from_bytes(&seed),
            )))
        })
        .transpose();
    let invalid_claim = parsed_claim.is_err();
    let claim = parsed_claim.unwrap_or_default().flatten();
    use crate::identity::TxSigner;

    // A missing or partly erased key is resumable only after the matching signer
    // configuration has been durably installed by a previous attempt.
    let mut resumed_during_erase = false;
    let mut enrollment = None;
    let mut last_failure = "chain-unavailable";
    for pause in [0, 1, 2, 4, 8, 16, 32] {
        if pause > 0 {
            status(std::slice::from_ref(directory), last_failure);
            tokio::time::sleep(Duration::from_secs(pause)).await;
        }
        let attempt = async {
            sui.validate_deployment().await?;
            let policy = sui
                .validate_enrollment_policy(&policy_id, &operator)
                .await?;
            if let Some((existing, original_claim)) = sui
                .previous_enrollment(&policy_id, &operator, trust.history_start_checkpoint)
                .await?
            {
                if let Some(claim) = &claim {
                    anyhow::ensure!(
                        claim.address() == original_claim,
                        Failure("device-already-enrolled-with-another-claim")
                    );
                } else {
                    resumed_during_erase = true;
                }
                sui.validate_cap(&existing.device_cap_id, &existing.device_object_id)
                    .await?;
                return Ok::<_, anyhow::Error>(existing);
            }
            if root.join("signer.toml").symlink_metadata().is_ok() {
                return Err(Failure("existing-signer-without-enrollment").into());
            }
            let claim = claim.as_ref().ok_or(Failure(if invalid_claim {
                "invalid-claim-key"
            } else {
                "claim-key-missing"
            }))?;
            if !sui
                .check_enrollment_limits(&policy_id, &policy, trust.managed.is_some())
                .await?
            {
                return Err(Failure("policy-closed-expired-or-exhausted").into());
            }
            sui.check_unused_claim(&policy, &claim.address())
                .await
                .context(Failure("claim-unavailable-or-consumed"))?;
            let submitted = sui
                .enroll(
                    &policy_id,
                    &enroll.name,
                    std::env::consts::ARCH,
                    claim,
                    trust.managed.as_ref(),
                    None,
                )
                .await
                .context(Failure("submission-unconfirmed"))?;
            sui.validate_cap(&submitted.device_cap_id, &submitted.device_object_id)
                .await
                .context(Failure("enrolled-cap-not-yet-readable"))?;
            Ok(submitted)
        };
        match tokio::time::timeout(Duration::from_secs(20), attempt).await {
            Ok(Ok(value)) => {
                enrollment = Some(value);
                break;
            }
            Ok(Err(error)) => {
                last_failure = error
                    .downcast_ref::<Failure>()
                    .map_or("chain-unavailable-or-invalid-deployment", |f| f.0);
                if matches!(
                    last_failure,
                    "device-already-enrolled-with-another-claim"
                        | "claim-key-missing"
                        | "invalid-claim-key"
                        | "existing-signer-without-enrollment"
                ) {
                    return Err(Failure(last_failure).into());
                }
            }
            Err(_) => last_failure = "chain-request-timed-out",
        }
    }
    let enrolled = enrollment.ok_or(Failure(last_failure))?;
    signer.insert(
        "device_cap_id".into(),
        enrolled.device_cap_id.clone().into(),
    );
    signer.insert(
        "keypair_path".into(),
        keypair_path.display().to_string().into(),
    );
    let signer = toml::to_string(&signer)?;
    anyhow::ensure!(
        check_existing(root, "signer.toml", signer.as_bytes(), signer_gid)?
            || !resumed_during_erase,
        Failure("claim-key-missing")
    );
    install_once(root, "deployment.json", &manifest, daemon_gid)?;
    install_once(root, "signer.toml", signer.as_bytes(), signer_gid)?;
    device.insert(
        "device_object_id".into(),
        toml::Value::String(enrolled.device_object_id),
    );
    device.insert(
        "device_cap_id".into(),
        toml::Value::String(enrolled.device_cap_id),
    );
    template.insert("device".into(), toml::Value::Table(device));

    if let Some(mut file) = claim_file {
        let length = file.metadata()?.len();
        let current = fs::symlink_metadata(&claim_path)?;
        anyhow::ensure!(
            current.is_file()
                && current.ino() == file.metadata()?.ino()
                && current.dev() == file.metadata()?.dev(),
            "claim file changed"
        );
        file.seek(SeekFrom::Start(0))?;
        file.write_all(&vec![0; length as usize])?;
        file.sync_all()?;
        fs::remove_file(claim_path)?;
        directory.sync_all()?;
    }
    install_once(
        root,
        "config.toml",
        toml::to_string(&template)?.as_bytes(),
        daemon_gid,
    )
}

fn valid_name(name: &str) -> bool {
    (1..=64).contains(&name.len())
        && name.as_bytes()[0].is_ascii_alphanumeric()
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

fn group_id(group: &str) -> Result<u32> {
    let group = std::ffi::CString::new(group)?;
    // SAFETY: called sequentially and the static record is copied immediately.
    let record =
        unsafe { libc::getgrnam(group.as_ptr()).as_ref() }.context("missing service group")?;
    Ok(record.gr_gid)
}

fn check_existing(root: &Path, name: &str, bytes: &[u8], gid: u32) -> Result<bool> {
    let existing = match OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(root.join(name))
    {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    let metadata = existing.metadata()?;
    anyhow::ensure!(
        metadata.is_file()
            && metadata.uid() == 0
            && metadata.gid() == gid
            && metadata.mode() & 0o777 == 0o640
            && read(&existing, bytes.len() as u64)? == bytes,
        "existing configuration differs"
    );
    Ok(true)
}

fn install_once(root: &Path, name: &str, bytes: &[u8], gid: u32) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let dest = root.join(name);
    let temp = root.join(format!(".first-boot-{name}"));
    // The directory is root-controlled and this unit is serialized by systemd.
    if let Err(error) = fs::remove_file(&temp) {
        if error.kind() != std::io::ErrorKind::NotFound {
            return Err(error.into());
        }
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temp)?;
    let result = (|| -> Result<()> {
        file.write_all(bytes)?;
        // SAFETY: file descriptor remains open for this call.
        if unsafe { libc::fchown(file.as_raw_fd(), 0, gid) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        file.set_permissions(fs::Permissions::from_mode(0o640))?;
        file.sync_all()?;
        if let Err(error) = fs::hard_link(&temp, &dest) {
            if error.kind() != std::io::ErrorKind::AlreadyExists {
                return Err(error.into());
            }
            check_existing(root, name, bytes, gid)?;
        }
        File::open(root)?.sync_all()?;
        Ok(())
    })();
    fs::remove_file(temp)?;
    result
}
