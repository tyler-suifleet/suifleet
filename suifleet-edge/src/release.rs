//! Release bundle verification (SF-039): `suifleet --verify-release BUNDLE TRUST`.
//!
//! A bundle (scripts/release.py) holds `release.json`, the files it lists, and
//! `release.sig.json` with one or more ed25519 signatures over
//! `DOMAIN || release.json`. A bundle is trusted only if at least one
//! signature verifies under a key in the device's root-owned trust file that
//! is not revoked, and every listed file matches its size and SHA-256.
//! Several signatures let a release be signed by the old and new key while a
//! signing key is rotated; revoking a key in the trust file ignores it.
use ed25519_dalek::{Signature, VerifyingKey};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::path::Path;

/// Domain separation: a release signature can never be mistaken for (or
/// replayed as) a signature over anything else, e.g. a Sui transaction.
pub const DOMAIN: &[u8] = b"suifleet-release-v1\n";
pub const MANIFEST: &str = "release.json";
pub const SIGNATURES: &str = "release.sig.json";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TrustFile {
    keys: Vec<TrustedKey>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TrustedKey {
    id: String,
    /// 32-byte ed25519 public key, hex.
    public_key: String,
    #[serde(default)]
    revoked: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Signatures {
    signatures: Vec<SignatureEntry>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SignatureEntry {
    key_id: String,
    /// 64-byte ed25519 signature, hex.
    signature: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema: u64,
    name: String,
    version: String,
    commit: String,
    target: String,
    files: Vec<FileEntry>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FileEntry {
    path: String,
    sha256: String,
    size: u64,
}

#[derive(Debug, PartialEq)]
pub struct Verified {
    pub name: String,
    pub version: String,
    pub commit: String,
    pub target: String,
    pub signer: String,
}

pub fn verify(bundle: &Path, trust: &Path) -> anyhow::Result<Verified> {
    let trust: TrustFile = toml::from_str(&std::fs::read_to_string(trust)?)
        .map_err(|e| anyhow::anyhow!("invalid trust file: {e}"))?;
    let mut ids = std::collections::BTreeSet::new();
    for key in &trust.keys {
        anyhow::ensure!(ids.insert(&key.id), "trust file lists key {} twice", key.id);
    }
    let manifest_bytes = std::fs::read(bundle.join(MANIFEST))?;
    let signatures: Signatures = serde_json::from_slice(&std::fs::read(bundle.join(SIGNATURES))?)
        .map_err(|e| anyhow::anyhow!("invalid {SIGNATURES}: {e}"))?;
    let mut message = DOMAIN.to_vec();
    message.extend_from_slice(&manifest_bytes);

    let mut reasons = Vec::new();
    let mut signer = None;
    for entry in &signatures.signatures {
        let Some(key) = trust.keys.iter().find(|k| k.id == entry.key_id) else {
            reasons.push(format!("{}: not a trusted key", entry.key_id));
            continue;
        };
        if key.revoked {
            reasons.push(format!("{}: revoked", entry.key_id));
            continue;
        }
        match check_signature(&key.public_key, &entry.signature, &message) {
            Ok(()) => {
                signer = Some(entry.key_id.clone());
                break;
            }
            Err(e) => reasons.push(format!("{}: {e}", entry.key_id)),
        }
    }
    let signer = signer.ok_or_else(|| {
        if reasons.is_empty() {
            anyhow::anyhow!("release carries no signatures")
        } else {
            anyhow::anyhow!("release is not signed by a trusted key ({})", reasons.join("; "))
        }
    })?;

    let manifest: Manifest = serde_json::from_slice(&manifest_bytes)
        .map_err(|e| anyhow::anyhow!("invalid {MANIFEST}: {e}"))?;
    anyhow::ensure!(manifest.schema == 1, "unsupported release schema {}", manifest.schema);
    anyhow::ensure!(!manifest.files.is_empty(), "release lists no files");
    for file in &manifest.files {
        anyhow::ensure!(
            !file.path.is_empty()
                && !file.path.starts_with('.')
                && !file.path.contains('/')
                && !file.path.contains('\\')
                && file.path != MANIFEST
                && file.path != SIGNATURES,
            "release lists an unsafe path {:?}",
            file.path
        );
        let path = bundle.join(&file.path);
        let metadata = std::fs::symlink_metadata(&path)
            .map_err(|e| anyhow::anyhow!("{}: {e}", file.path))?;
        anyhow::ensure!(metadata.is_file(), "{} is not a regular file", file.path);
        anyhow::ensure!(
            metadata.len() == file.size,
            "{}: size {} differs from the signed {}",
            file.path,
            metadata.len(),
            file.size
        );
        let actual = hex::encode(Sha256::digest(std::fs::read(&path)?));
        anyhow::ensure!(
            actual.eq_ignore_ascii_case(&file.sha256),
            "{}: SHA-256 {actual} differs from the signed {}",
            file.path,
            file.sha256
        );
    }
    Ok(Verified {
        name: manifest.name,
        version: manifest.version,
        commit: manifest.commit,
        target: manifest.target,
        signer,
    })
}

fn check_signature(public_key: &str, signature: &str, message: &[u8]) -> anyhow::Result<()> {
    let key: [u8; 32] = hex::decode(public_key)?
        .try_into()
        .map_err(|_| anyhow::anyhow!("public key must be 32 bytes"))?;
    let signature: [u8; 64] = hex::decode(signature)?
        .try_into()
        .map_err(|_| anyhow::anyhow!("signature must be 64 bytes"))?;
    VerifyingKey::from_bytes(&key)?
        .verify_strict(message, &Signature::from_bytes(&signature))
        .map_err(|_| anyhow::anyhow!("signature does not verify"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use serde_json::json;

    struct Bundle {
        dir: std::path::PathBuf,
    }

    impl Bundle {
        fn new(name: &str) -> Self {
            let dir = std::env::temp_dir().join(format!("suifleet-release-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(dir.join("artifact.tar.gz"), b"daemon bytes").unwrap();
            std::fs::write(dir.join("sbom.spdx.json"), b"{}").unwrap();
            let files: Vec<_> = ["artifact.tar.gz", "sbom.spdx.json"]
                .iter()
                .map(|name| {
                    let bytes = std::fs::read(dir.join(name)).unwrap();
                    json!({"path": name, "sha256": hex::encode(Sha256::digest(&bytes)), "size": bytes.len()})
                })
                .collect();
            let manifest = json!({"schema": 1, "name": "suifleet", "version": "0.1.0",
                "commit": "abc", "target": "aarch64-unknown-linux-gnu", "files": files});
            std::fs::write(dir.join(MANIFEST), serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();
            Self { dir }
        }
        fn sign(&self, keys: &[(&str, &SigningKey)]) {
            let mut message = DOMAIN.to_vec();
            message.extend(std::fs::read(self.dir.join(MANIFEST)).unwrap());
            let signatures: Vec<_> = keys
                .iter()
                .map(|(id, key)| json!({"key_id": id, "signature": hex::encode(key.sign(&message).to_bytes())}))
                .collect();
            std::fs::write(self.dir.join(SIGNATURES), json!({"signatures": signatures}).to_string()).unwrap();
        }
        fn trust(&self, keys: &[(&str, &SigningKey, bool)]) -> std::path::PathBuf {
            let mut text = String::new();
            for (id, key, revoked) in keys {
                text += &format!(
                    "[[keys]]\nid = \"{id}\"\npublic_key = \"{}\"\nrevoked = {revoked}\n",
                    hex::encode(key.verifying_key().to_bytes())
                );
            }
            let path = self.dir.join("trust.toml");
            std::fs::write(&path, text).unwrap();
            path
        }
    }

    fn keys() -> (SigningKey, SigningKey, SigningKey) {
        (SigningKey::from_bytes(&[1; 32]), SigningKey::from_bytes(&[2; 32]), SigningKey::from_bytes(&[3; 32]))
    }

    #[test]
    fn trusted_signature_and_intact_files_verify() {
        let (old, _, _) = keys();
        let bundle = Bundle::new("ok");
        bundle.sign(&[("release-a", &old)]);
        let trust = bundle.trust(&[("release-a", &old, false)]);
        let verified = verify(&bundle.dir, &trust).unwrap();
        assert_eq!((verified.version.as_str(), verified.signer.as_str()), ("0.1.0", "release-a"));
    }

    #[test]
    fn tampered_artifact_is_rejected() {
        let (old, _, _) = keys();
        let bundle = Bundle::new("artifact");
        bundle.sign(&[("release-a", &old)]);
        let trust = bundle.trust(&[("release-a", &old, false)]);
        std::fs::write(bundle.dir.join("artifact.tar.gz"), b"daemon bytez").unwrap();
        assert!(verify(&bundle.dir, &trust).unwrap_err().to_string().contains("SHA-256"));
        std::fs::write(bundle.dir.join("artifact.tar.gz"), b"longer daemon bytes").unwrap();
        assert!(verify(&bundle.dir, &trust).unwrap_err().to_string().contains("size"));
    }

    #[test]
    fn tampered_manifest_is_rejected() {
        let (old, _, _) = keys();
        let bundle = Bundle::new("manifest");
        bundle.sign(&[("release-a", &old)]);
        let trust = bundle.trust(&[("release-a", &old, false)]);
        let text = std::fs::read_to_string(bundle.dir.join(MANIFEST)).unwrap();
        std::fs::write(bundle.dir.join(MANIFEST), text.replace("0.1.0", "0.1.1")).unwrap();
        assert!(verify(&bundle.dir, &trust).unwrap_err().to_string().contains("does not verify"));
    }

    #[test]
    fn untrusted_and_revoked_signers_are_rejected() {
        let (old, new, stranger) = keys();
        let bundle = Bundle::new("signers");
        bundle.sign(&[("release-x", &stranger)]);
        let trust = bundle.trust(&[("release-a", &old, false)]);
        assert!(verify(&bundle.dir, &trust).unwrap_err().to_string().contains("not a trusted key"));
        // A known ID whose key does not match the signature.
        bundle.sign(&[("release-a", &new)]);
        assert!(verify(&bundle.dir, &trust).unwrap_err().to_string().contains("does not verify"));
        bundle.sign(&[("release-a", &old)]);
        let revoked = bundle.trust(&[("release-a", &old, true)]);
        assert!(verify(&bundle.dir, &revoked).unwrap_err().to_string().contains("revoked"));
    }

    #[test]
    fn rotation_accepts_the_new_key_and_drops_the_revoked_one() {
        let (old, new, _) = keys();
        let bundle = Bundle::new("rotation");
        // During rotation a release carries both signatures.
        bundle.sign(&[("release-a", &old), ("release-b", &new)]);
        let both = bundle.trust(&[("release-a", &old, false), ("release-b", &new, false)]);
        assert_eq!(verify(&bundle.dir, &both).unwrap().signer, "release-a");
        let rotated = bundle.trust(&[("release-a", &old, true), ("release-b", &new, false)]);
        assert_eq!(verify(&bundle.dir, &rotated).unwrap().signer, "release-b");
        // After rotation, a release signed only by the revoked key is refused.
        bundle.sign(&[("release-a", &old)]);
        assert!(verify(&bundle.dir, &rotated).is_err());
    }

    #[test]
    fn unsafe_paths_and_duplicate_keys_are_rejected() {
        let (old, _, _) = keys();
        let bundle = Bundle::new("paths");
        for path in ["../etc/passwd", "sub/file", ".hidden", "release.sig.json", ""] {
            let manifest = json!({"schema": 1, "name": "suifleet", "version": "0.1.0", "commit": "abc",
                "target": "aarch64-unknown-linux-gnu", "files": [{"path": path, "sha256": "00", "size": 0}]});
            std::fs::write(bundle.dir.join(MANIFEST), manifest.to_string()).unwrap();
            bundle.sign(&[("release-a", &old)]);
            let trust = bundle.trust(&[("release-a", &old, false)]);
            assert!(verify(&bundle.dir, &trust).unwrap_err().to_string().contains("unsafe path"), "{path}");
        }
        let duplicate = bundle.trust(&[("release-a", &old, false), ("release-a", &old, false)]);
        assert!(verify(&bundle.dir, &duplicate).unwrap_err().to_string().contains("twice"));
    }

    #[test]
    fn domain_separation_rejects_a_bare_manifest_signature() {
        let (old, _, _) = keys();
        let bundle = Bundle::new("domain");
        let bare = old.sign(&std::fs::read(bundle.dir.join(MANIFEST)).unwrap());
        std::fs::write(
            bundle.dir.join(SIGNATURES),
            json!({"signatures": [{"key_id": "release-a", "signature": hex::encode(bare.to_bytes())}]}).to_string(),
        )
        .unwrap();
        let trust = bundle.trust(&[("release-a", &old, false)]);
        assert!(verify(&bundle.dir, &trust).is_err());
    }
}
