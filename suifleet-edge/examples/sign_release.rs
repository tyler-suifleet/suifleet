//! Release signing tool (SF-039). Never part of the device binary; run it
//! only where the release signing key lives (the protected release job, or an
//! offline machine).
//!
//!   sign_release generate KEY_FILE          new key (hex seed, mode 0600); prints the public key
//!   sign_release public KEY_FILE            prints the public key for the trust file
//!   sign_release sign KEY_FILE KEY_ID DIR   adds/replaces KEY_ID's signature in DIR/release.sig.json
//!
//! The signature covers `suifleet-release-v1\n || release.json`; the daemon
//! verifies it with `suifleet --verify-release DIR TRUST_FILE`.
use ed25519_dalek::{Signer, SigningKey};
use serde_json::{json, Value};
use std::{io::Write, path::Path};

const DOMAIN: &[u8] = b"suifleet-release-v1\n";

fn load(path: &Path) -> anyhow::Result<SigningKey> {
    let seed: [u8; 32] = hex::decode(std::fs::read_to_string(path)?.trim())?
        .try_into()
        .map_err(|_| anyhow::anyhow!("key file must hold 32 hex bytes"))?;
    Ok(SigningKey::from_bytes(&seed))
}

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        ["generate", path] => {
            use std::os::unix::fs::OpenOptionsExt;
            let mut seed = [0u8; 32];
            getrandom(&mut seed)?;
            let key = SigningKey::from_bytes(&seed);
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(path)?;
            writeln!(file, "{}", hex::encode(seed))?;
            println!("{}", hex::encode(key.verifying_key().to_bytes()));
        }
        ["public", path] => {
            println!("{}", hex::encode(load(Path::new(path))?.verifying_key().to_bytes()));
        }
        ["sign", path, key_id, dir] => {
            let key = load(Path::new(path))?;
            let dir = Path::new(dir);
            let mut message = DOMAIN.to_vec();
            message.extend(std::fs::read(dir.join("release.json"))?);
            let signature = hex::encode(key.sign(&message).to_bytes());
            let file = dir.join("release.sig.json");
            let mut document: Value = match std::fs::read(&file) {
                Ok(bytes) => serde_json::from_slice(&bytes)?,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => json!({"signatures": []}),
                Err(e) => return Err(e.into()),
            };
            let signatures = document["signatures"]
                .as_array_mut()
                .ok_or_else(|| anyhow::anyhow!("malformed release.sig.json"))?;
            signatures.retain(|entry| entry["key_id"] != **key_id);
            signatures.push(json!({"key_id": key_id, "signature": signature}));
            std::fs::write(&file, serde_json::to_string_pretty(&document)? + "\n")?;
            println!("signed release.json as {key_id}");
        }
        _ => anyhow::bail!("usage: sign_release generate KEY_FILE | public KEY_FILE | sign KEY_FILE KEY_ID DIR"),
    }
    Ok(())
}

fn getrandom(buffer: &mut [u8]) -> anyhow::Result<()> {
    use std::io::Read;
    std::fs::File::open("/dev/urandom")?.read_exact(buffer)?;
    Ok(())
}
