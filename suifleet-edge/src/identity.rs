use blake2::{digest::consts::U32, Blake2b, Digest};
use ed25519_dalek::{Signer, SigningKey};
use rand::rngs::OsRng;
use std::{fs::OpenOptions, io::Write, path::Path};

pub fn load(path: &Path) -> anyhow::Result<SigningKey> {
    let bytes = hex::decode(std::fs::read_to_string(path)?.trim())?;
    let key: [u8; 32] = bytes
        .try_into()
        .map_err(|_| anyhow::anyhow!("key must contain 32 hex bytes"))?;
    Ok(SigningKey::from_bytes(&key))
}

pub fn hash(bytes: &[u8]) -> Vec<u8> {
    Blake2b::<U32>::digest(bytes).to_vec()
}

pub fn address(key: &SigningKey) -> String {
    let mut bytes = vec![0];
    bytes.extend_from_slice(&key.verifying_key().to_bytes());
    format!("0x{}", hex::encode(hash(&bytes)))
}

pub fn signature(key: &SigningKey, tx: &[u8]) -> Vec<u8> {
    let mut intent = vec![0, 0, 0];
    intent.extend_from_slice(tx);
    let mut out = vec![0];
    out.extend_from_slice(&key.sign(&hash(&intent)).to_bytes());
    out.extend_from_slice(&key.verifying_key().to_bytes());
    out
}

/// First-boot provisioning is idempotent and never overwrites an existing key.
pub fn provision(path: &Path) -> anyhow::Result<String> {
    if path.exists() {
        return Ok(address(&load(path)?));
    }
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    publish_key(path, |file| {
        let key = SigningKey::generate(&mut OsRng);
        writeln!(file, "{}", hex::encode(key.to_bytes()))?;
        Ok(())
    })
}

// A private staging file can be incomplete after interruption; the public path
// only becomes visible after its complete contents have reached the filesystem.
fn publish_key(
    path: &Path,
    write_key: impl FnOnce(&mut std::fs::File) -> anyhow::Result<()>,
) -> anyhow::Result<String> {
    use rand::RngCore;
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let mut nonce = [0u8; 16];
    OsRng.fill_bytes(&mut nonce);
    let temporary = parent.join(format!(".device-key.{}", hex::encode(nonce)));
    struct Cleanup(std::path::PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
        }
    }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temporary)?;
    let cleanup = Cleanup(temporary.clone());
    write_key(&mut file)?;
    file.sync_all()?;
    // hard_link is atomic and refuses to replace even a concurrently created
    // destination. Both names are on the same filesystem.
    match std::fs::hard_link(&temporary, path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error.into()),
    }
    drop(cleanup);
    std::fs::File::open(parent)?.sync_all()?;
    Ok(address(&load(path)?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signature, Verifier};

    #[test]
    fn interrupted_key_write_leaves_no_published_key_and_can_retry() {
        let root = std::env::temp_dir().join(format!("suifleet-key-{}", rand::random::<u128>()));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("device.key");
        let result = publish_key(&path, |file| {
            file.write_all(b"partial")?;
            anyhow::bail!("simulated interrupted write")
        });
        assert!(result.is_err());
        assert!(!path.exists());
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 0);
        // Abrupt termination may leave staging bytes, which are never loaded.
        std::fs::write(root.join(".device-key.interrupted"), b"partial").unwrap();
        let address = provision(&path).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(provision(&path).unwrap(), address);
        assert_eq!(std::fs::read(&path).unwrap(), bytes);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
        // A competing provisioner must preserve the winner's key.
        publish_key(&path, |file| {
            writeln!(file, "{}", "01".repeat(32))?;
            Ok(())
        })
        .unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), bytes);
        std::fs::write(&path, b"corrupt").unwrap();
        assert!(provision(&path).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), b"corrupt");
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn signs_intent_digest_and_serializes_sui_signature() {
        let key = SigningKey::from_bytes(&[7; 32]);
        let tx = [1, 2, 3, 4];
        let signed = signature(&key, &tx);
        assert_eq!(signed.len(), 97);
        assert_eq!(signed[0], 0);
        assert_eq!(&signed[65..], &key.verifying_key().to_bytes());
        let sig = Signature::from_slice(&signed[1..65]).unwrap();
        assert!(key
            .verifying_key()
            .verify(&hash(&[0, 0, 0, 1, 2, 3, 4]), &sig)
            .is_ok());
        assert!(key
            .verifying_key()
            .verify(&[0, 0, 0, 1, 2, 3, 4], &sig)
            .is_err());
    }

    #[test]
    fn matches_mysten_sdk_identity_and_signature_vector() {
        use base64::{engine::general_purpose::STANDARD, Engine};
        let key = SigningKey::from_bytes(&[7; 32]);
        assert_eq!(
            address(&key),
            "0xa0ccc8bcc83f6c628340134f8546a21e0618fd1aaa02432bba454c4a2c2233da"
        );
        assert_eq!(STANDARD.encode(signature(&key, &[1,2,3,4])), "AGe7jkxf/S/LN1HBGv3Lbbhb+4FzdWYtt+XlzfsikT+N6RHAAxUZBeipd/iHLQofYPVZqPaSLt4WQTkmA1YnEgXqSmxj4pxSCr71UHsTLsX5lUd2rr6+e5JCHuppFEbSLA==");
    }

    #[test]
    fn blake2b_256_vector() {
        assert_eq!(
            hex::encode(hash(b"")),
            "0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8"
        );
    }
}
