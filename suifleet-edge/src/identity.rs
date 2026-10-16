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

#[cfg(test)]
pub fn signature(key: &SigningKey, tx: &[u8]) -> Vec<u8> {
    digest_signature(key, &intent_digest(tx))
}

fn digest_signature(key: &SigningKey, digest: &[u8]) -> Vec<u8> {
    let mut out = vec![0];
    out.extend_from_slice(&key.sign(digest).to_bytes());
    out.extend_from_slice(&key.verifying_key().to_bytes());
    out
}

/// The Sui intent digest a transaction signature commits to:
/// Blake2b-256 of the intent prefix (transaction data, v0, Sui) and the bytes.
pub fn intent_digest(tx: &[u8]) -> Vec<u8> {
    let mut intent = vec![0, 0, 0];
    intent.extend_from_slice(tx);
    hash(&intent)
}

/// The digest a Sui personal-message signature commits to: Blake2b-256 of the
/// intent prefix (personal message, v0, Sui) and the BCS `vector<u8>`. It can
/// never be valid as a transaction signature (SF-019 telemetry).
pub fn personal_message_digest(message: &[u8]) -> Vec<u8> {
    let mut intent = vec![3, 0, 0];
    intent.extend(bcs::to_bytes(message).expect("bytes serialize"));
    hash(&intent)
}

/// Whatever signs this device's transactions (SF-021): a key file, or a
/// key held in a PKCS#11 token (HSM, TPM via a PKCS#11 provider).
pub trait TxSigner: Send + Sync {
    /// The Sui address of the signing key.
    fn address(&self) -> String;
    /// A serialized Sui `UserSignature` over BCS transaction data.
    fn sign(&self, tx: &[u8]) -> anyhow::Result<Vec<u8>> {
        self.sign_digest(&intent_digest(tx))
    }
    /// A serialized Sui `UserSignature` over a personal message.
    fn sign_personal_message(&self, message: &[u8]) -> anyhow::Result<Vec<u8>> {
        self.sign_digest(&personal_message_digest(message))
    }
    /// A serialized Sui `UserSignature` over an intent digest.
    fn sign_digest(&self, digest: &[u8]) -> anyhow::Result<Vec<u8>>;
}

/// An ed25519 key in a hex file (the default, `[device] keypair_path`).
pub struct Ed25519File(pub SigningKey);

impl TxSigner for Ed25519File {
    fn address(&self) -> String {
        address(&self.0)
    }
    fn sign_digest(&self, digest: &[u8]) -> anyhow::Result<Vec<u8>> {
        Ok(digest_signature(&self.0, digest))
    }
}

/// The configured signer: `[device.signer]` if present, else the key file.
pub fn signer(device: &crate::config::DeviceConfig) -> anyhow::Result<std::sync::Arc<dyn TxSigner>> {
    match &device.signer {
        None => Ok(std::sync::Arc::new(Ed25519File(load(&device.keypair_path)?))),
        Some(config) => signer_from(config),
    }
}

pub fn signer_from(config: &crate::config::SignerConfig) -> anyhow::Result<std::sync::Arc<dyn TxSigner>> {
    anyhow::ensure!(config.kind == "pkcs11", "unsupported signer kind {:?} (supported: pkcs11)", config.kind);
    #[cfg(feature = "pkcs11")]
    {
        Ok(std::sync::Arc::new(pkcs11::Pkcs11P256::open(config)?))
    }
    #[cfg(not(feature = "pkcs11"))]
    {
        anyhow::bail!("this build has no PKCS#11 support; rebuild with --features pkcs11")
    }
}

/// Sui secp256r1 (P-256) signatures, the scheme TPM 2.0 and common HSMs
/// support (most lack ed25519).
#[cfg(feature = "pkcs11")]
pub mod secp256r1 {
    use sha2::{Digest, Sha256};

    pub const FLAG: u8 = 0x02;

    /// Sui address of a compressed P-256 public key.
    pub fn address(compressed: &[u8; 33]) -> String {
        let mut bytes = vec![FLAG];
        bytes.extend_from_slice(compressed);
        format!("0x{}", hex::encode(super::hash(&bytes)))
    }

    /// The 32 bytes a raw-ECDSA (prehashed) signer must sign: Sui signs the
    /// intent digest with ECDSA-SHA256, i.e. ECDSA over SHA-256(digest).
    pub fn prehash(tx: &[u8]) -> [u8; 32] {
        prehash_digest(&super::intent_digest(tx))
    }

    /// The same for any intent digest (a personal message's, for example).
    pub fn prehash_digest(digest: &[u8]) -> [u8; 32] {
        Sha256::digest(digest).into()
    }

    /// Serialize a raw r||s signature as a Sui `UserSignature`, normalizing S
    /// to the lower half (Sui rejects malleable high-S signatures).
    pub fn serialize(rs: &[u8], compressed: &[u8; 33]) -> anyhow::Result<Vec<u8>> {
        let signature = p256::ecdsa::Signature::from_slice(rs)
            .map_err(|e| anyhow::anyhow!("invalid P-256 signature from signer: {e}"))?;
        let signature = signature.normalize_s().unwrap_or(signature);
        let mut out = vec![FLAG];
        out.extend_from_slice(&signature.to_bytes());
        out.extend_from_slice(compressed);
        Ok(out)
    }

    /// A SEC1 public key (uncompressed or compressed, optionally wrapped in
    /// a DER OCTET STRING as PKCS#11 CKA_EC_POINT returns it), compressed.
    pub fn compress(point: &[u8]) -> anyhow::Result<[u8; 33]> {
        let raw = match point {
            [0x04, len, rest @ ..] if *len as usize == rest.len() => rest,
            other => other,
        };
        let key = p256::PublicKey::from_sec1_bytes(raw)
            .map_err(|e| anyhow::anyhow!("invalid P-256 public key: {e}"))?;
        use p256::elliptic_curve::sec1::ToEncodedPoint;
        key.to_encoded_point(true)
            .as_bytes()
            .try_into()
            .map_err(|_| anyhow::anyhow!("unexpected compressed key length"))
    }
}

/// A P-256 private key held in a PKCS#11 token, used through its module
/// (e.g. SoftHSM2, a hardware HSM, or a TPM's PKCS#11 provider). The key
/// never leaves the token; each signature is a raw ECDSA operation (CKM_ECDSA)
/// over the Sui prehash.
#[cfg(feature = "pkcs11")]
pub mod pkcs11 {
    use cryptoki::context::{CInitializeArgs, Pkcs11};
    use cryptoki::mechanism::Mechanism;
    use cryptoki::object::{Attribute, AttributeType, ObjectClass};
    use cryptoki::session::UserType;
    use cryptoki::slot::Slot;
    use cryptoki::types::AuthPin;

    pub struct Pkcs11P256 {
        context: Pkcs11,
        slot: Slot,
        key_label: String,
        pin: String,
        compressed: [u8; 33],
    }

    impl Pkcs11P256 {
        pub fn open(config: &crate::config::SignerConfig) -> anyhow::Result<Self> {
            let context = Pkcs11::new(&config.module)
                .map_err(|e| anyhow::anyhow!("cannot load PKCS#11 module {}: {e}", config.module.display()))?;
            context.initialize(CInitializeArgs::OsThreads)?;
            let slot = context
                .get_slots_with_token()?
                .into_iter()
                .find(|slot| {
                    context.get_token_info(*slot).map(|info| info.label().trim() == config.token_label).unwrap_or(false)
                })
                .ok_or_else(|| anyhow::anyhow!("no PKCS#11 token labelled {:?}", config.token_label))?;
            let pin = std::fs::read_to_string(&config.pin_file)?.trim().to_owned();
            let mut signer = Self { context, slot, key_label: config.key_label.clone(), pin, compressed: [0; 33] };
            let session = signer.context.open_ro_session(signer.slot)?;
            let public = session
                .find_objects(&[Attribute::Class(ObjectClass::PUBLIC_KEY), Attribute::Label(signer.key_label.as_bytes().to_vec())])?
                .into_iter()
                .next()
                .ok_or_else(|| anyhow::anyhow!("no public key labelled {:?} in the token", signer.key_label))?;
            let point = match session.get_attributes(public, &[AttributeType::EcPoint])?.into_iter().next() {
                Some(Attribute::EcPoint(point)) => point,
                _ => anyhow::bail!("public key {:?} has no EC point (is it a P-256 key?)", signer.key_label),
            };
            signer.compressed = super::secp256r1::compress(&point)?;
            Ok(signer)
        }
    }

    impl super::TxSigner for Pkcs11P256 {
        fn address(&self) -> String {
            super::secp256r1::address(&self.compressed)
        }
        fn sign_digest(&self, digest: &[u8]) -> anyhow::Result<Vec<u8>> {
            let session = self.context.open_ro_session(self.slot)?;
            session.login(UserType::User, Some(&AuthPin::new(self.pin.clone())))?;
            let private = session
                .find_objects(&[Attribute::Class(ObjectClass::PRIVATE_KEY), Attribute::Label(self.key_label.as_bytes().to_vec())])?
                .into_iter()
                .next()
                .ok_or_else(|| anyhow::anyhow!("no private key labelled {:?} in the token", self.key_label))?;
            let rs = session.sign(&Mechanism::Ecdsa, private, &super::secp256r1::prehash_digest(digest))?;
            super::secp256r1::serialize(&rs, &self.compressed)
        }
    }
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

    #[cfg(feature = "pkcs11")]
    #[test]
    fn secp256r1_signatures_verify_with_mystens_independent_verifier() {
        use p256::ecdsa::{signature::hazmat::PrehashSigner, SigningKey as P256Key};
        use sui_crypto::SuiVerifier;
        let key = P256Key::from_slice(&[9; 32]).unwrap();
        let compressed = secp256r1::compress(key.verifying_key().to_encoded_point(false).as_bytes()).unwrap();
        // Address matches Mysten's SDK derivation.
        let public = sui_sdk_types::Secp256r1PublicKey::new(compressed);
        assert_eq!(secp256r1::address(&compressed), public.derive_address().to_string());
        // A CKA_EC_POINT-style DER OCTET STRING wrapping decodes the same.
        let uncompressed = key.verifying_key().to_encoded_point(false);
        let mut der = vec![0x04, uncompressed.as_bytes().len() as u8];
        der.extend_from_slice(uncompressed.as_bytes());
        assert_eq!(secp256r1::compress(&der).unwrap(), compressed);
        // A raw prehash signature, serialized, verifies as a Sui transaction signature.
        let sender: sui_sdk_types::Address = secp256r1::address(&compressed).parse().unwrap();
        let transaction = sui_sdk_types::Transaction {
            kind: sui_sdk_types::TransactionKind::ProgrammableTransaction(sui_sdk_types::ProgrammableTransaction {
                inputs: vec![], commands: vec![] }),
            sender,
            gas_payment: sui_sdk_types::GasPayment { objects: vec![], owner: sender, price: 1000, budget: 5_000_000 },
            expiration: sui_sdk_types::TransactionExpiration::None,
        };
        let bytes = bcs::to_bytes(&transaction).unwrap();
        let rs: p256::ecdsa::Signature = key.sign_prehash(&secp256r1::prehash(&bytes)).unwrap();
        let serialized = secp256r1::serialize(&rs.to_bytes(), &compressed).unwrap();
        let signature = sui_sdk_types::UserSignature::from_bytes(&serialized).unwrap();
        sui_crypto::UserSignatureVerifier::new().verify_transaction(&transaction, &signature).unwrap();
        // A high-S signature is normalized before serialization.
        let (r, s) = rs.split_scalars();
        let high = p256::ecdsa::Signature::from_scalars(r, -*s).unwrap();
        assert_eq!(secp256r1::serialize(&high.to_bytes(), &compressed).unwrap(), serialized);
        // Signing different bytes does not verify.
        let mut other = transaction.clone();
        other.gas_payment.budget += 1;
        assert!(sui_crypto::UserSignatureVerifier::new().verify_transaction(&other, &signature).is_err());
    }

    #[test]
    fn personal_messages_verify_with_mystens_verifier_and_never_as_transactions() {
        use sui_crypto::SuiVerifier;
        let signer = Ed25519File(SigningKey::from_bytes(&[7; 32]));
        let message = br#"{"schema":1}"#;
        let signed = signer.sign_personal_message(message).unwrap();
        let signature = sui_sdk_types::UserSignature::from_bytes(&signed).unwrap();
        let verifier = sui_crypto::UserSignatureVerifier::new();
        let personal = sui_sdk_types::PersonalMessage(message.as_slice().into());
        verifier.verify_personal_message(&personal, &signature).unwrap();
        let other = sui_sdk_types::PersonalMessage(br#"{"schema":2}"#.as_slice().into());
        assert!(verifier.verify_personal_message(&other, &signature).is_err());
        // The same bytes signed as a transaction give another signature.
        assert_ne!(signer.sign(message).unwrap(), signed);
        assert_ne!(personal_message_digest(message), intent_digest(message));
    }

    #[cfg(feature = "pkcs11")]
    #[test]
    fn secp256r1_personal_messages_verify_with_mystens_verifier() {
        use p256::ecdsa::{signature::hazmat::PrehashSigner, SigningKey as P256Key};
        use sui_crypto::SuiVerifier;
        let key = P256Key::from_slice(&[9; 32]).unwrap();
        let compressed = secp256r1::compress(key.verifying_key().to_encoded_point(false).as_bytes()).unwrap();
        let message = b"telemetry";
        let digest = personal_message_digest(message);
        let rs: p256::ecdsa::Signature = key.sign_prehash(&secp256r1::prehash_digest(&digest)).unwrap();
        let serialized = secp256r1::serialize(&rs.to_bytes(), &compressed).unwrap();
        let signature = sui_sdk_types::UserSignature::from_bytes(&serialized).unwrap();
        let personal = sui_sdk_types::PersonalMessage(message.as_slice().into());
        sui_crypto::UserSignatureVerifier::new().verify_personal_message(&personal, &signature).unwrap();
    }

    #[test]
    fn ed25519_file_signer_matches_the_existing_signature() {
        let key = SigningKey::from_bytes(&[7; 32]);
        let signer = Ed25519File(key.clone());
        assert_eq!(signer.address(), address(&key));
        assert_eq!(signer.sign(&[1, 2, 3, 4]).unwrap(), signature(&key, &[1, 2, 3, 4]));
    }

    #[test]
    fn blake2b_256_vector() {
        assert_eq!(
            hex::encode(hash(b"")),
            "0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8"
        );
    }
}
