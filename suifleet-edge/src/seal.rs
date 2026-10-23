//! Seal threshold decryption (SF-022): the envelope format and cryptography.
//!
//! A confidential Walrus blob is `MAGIC || EncryptedObject`, where
//! `EncryptedObject` is Seal's BCS format (version 0, Boneh-Franklin IBE over
//! BLS12-381 with a threshold of key servers, then AES-256-GCM or HMAC-SHA3-256
//! CTR). This is an independent implementation of Mysten's Seal client; the
//! tests decrypt objects produced by the pinned `@mysten/seal` SDK
//! (`interfaces/seal-vectors.json`), so the two must agree byte for byte.
//!
//! Binding: the DEM's associated data is `aad(kind, plaintext SHA-256)`. The
//! on-chain deployment records the plaintext digests, so after decryption the
//! daemon still checks the plaintext against them. Anyone can encrypt to a
//! Seal identity, so a ciphertext alone authenticates nothing.
//!
//! Public: the magic, package and identity (policy ID, key epoch, nonce), key
//! server object IDs and threshold, the AAD (kind and plaintext SHA-256), and
//! the ciphertext length. A plaintext digest lets anyone confirm a guess of
//! low-entropy content.
use ark_bls12_381::{Bls12_381, Fq, Fq12, Fr, G1Affine, G1Projective, G2Affine, G2Projective};
use ark_ec::{
    hashing::{curve_maps::wb::WBMap, map_to_curve_hasher::MapToCurveBasedHasher, HashToCurve},
    pairing::{Pairing, PairingOutput},
    AffineRepr, CurveGroup, PrimeGroup,
};
use ark_ff::{field_hashers::DefaultFieldHasher, BigInteger, PrimeField, Zero};
use ark_serialize::{CanonicalDeserialize, CanonicalSerialize};
use sha3::{Digest, Sha3_256};
use std::{collections::HashMap, ops::Range};

/// Prefix of every SuiFleet confidential blob.
pub const MAGIC: &[u8] = b"suifleet/seal/v1\n";
/// Seal identities: policy ID (32) || key epoch (u64 LE) || nonce (16..=64).
pub const PREFIX_LENGTH: usize = 40;
pub const MIN_NONCE: usize = 16;
pub const MAX_NONCE: usize = 64;

const BLS_DST: &[u8] = b"BLS_SIG_BLS12381G1_XMD:SHA-256_SSWU_RO_NUL_";
const IBE_DST: &[u8] = b"SUI-SEAL-IBE-BLS12381-00";
const KDF_DST: &[u8] = b"SUI-SEAL-IBE-BLS12381-H2-00";
const DERIVE_KEY_DST: &[u8] = b"SUI-SEAL-IBE-BLS12381-H3-00";
pub const POP_DST: &[u8] = b"SUI-SEAL-IBE-BLS12381-POP-00";
/// Seal's fixed AES-GCM IV: every DEM key is used once.
const AES_IV: [u8; 16] = [138, 55, 153, 253, 198, 46, 121, 219, 160, 128, 89, 7, 214, 156, 148, 220];

/// What a blob holds; part of the associated data.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Artifact,
    Recipe,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Artifact => "artifact",
            Kind::Recipe => "recipe",
        }
    }
}

/// The DEM associated data binding a ciphertext to its kind and plaintext.
pub fn aad(kind: Kind, plaintext_sha256: &str) -> Vec<u8> {
    format!("suifleet/seal/v1/{}/{}", kind.as_str(), plaintext_sha256.to_ascii_lowercase()).into_bytes()
}

pub fn is_envelope(bytes: &[u8]) -> bool {
    bytes.starts_with(MAGIC)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Dem {
    Aes256Gcm,
    Hmac256Ctr { mac: [u8; 32] },
}

/// A parsed Seal `EncryptedObject`; `blob` locates the DEM ciphertext in the
/// buffer it was parsed from, so large artifacts decrypt in place.
#[derive(Debug, Clone)]
pub struct Object {
    pub package_id: [u8; 32],
    pub id: Vec<u8>,
    pub services: Vec<([u8; 32], u8)>,
    pub threshold: u8,
    pub nonce: G2Affine,
    nonce_bytes: [u8; 96],
    pub encrypted_shares: Vec<[u8; 32]>,
    encrypted_randomness: [u8; 32],
    pub dem: Dem,
    pub aad: Vec<u8>,
    pub blob: Range<usize>,
}

impl Object {
    /// The Seal key ID: package ID || identity.
    pub fn full_id(&self) -> Vec<u8> {
        [self.package_id.as_slice(), &self.id].concat()
    }
    /// The access policy named by the identity, when it has our layout.
    pub fn policy_id(&self) -> Option<[u8; 32]> {
        (PREFIX_LENGTH + MIN_NONCE..=PREFIX_LENGTH + MAX_NONCE)
            .contains(&self.id.len())
            .then(|| self.id[..32].try_into().unwrap())
    }
    pub fn key_epoch(&self) -> Option<u64> {
        self.policy_id().map(|_| u64::from_le_bytes(self.id[32..40].try_into().unwrap()))
    }
}

struct Reader<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, n: usize) -> anyhow::Result<&'a [u8]> {
        let end = self.at.checked_add(n).filter(|end| *end <= self.bytes.len());
        let end = end.ok_or_else(|| anyhow::anyhow!("truncated Seal object"))?;
        let out = &self.bytes[self.at..end];
        self.at = end;
        Ok(out)
    }
    fn array<const N: usize>(&mut self) -> anyhow::Result<[u8; N]> {
        Ok(self.take(N)?.try_into().unwrap())
    }
    fn byte(&mut self) -> anyhow::Result<u8> {
        Ok(self.take(1)?[0])
    }
    /// BCS ULEB128 length, bounded to what remains.
    fn length(&mut self) -> anyhow::Result<usize> {
        let mut value: u64 = 0;
        for shift in (0..64).step_by(7) {
            let byte = self.byte()?;
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                anyhow::ensure!(value <= (self.bytes.len() - self.at) as u64, "Seal object length out of range");
                return Ok(value as usize);
            }
        }
        anyhow::bail!("invalid Seal object length")
    }
    fn vector(&mut self) -> anyhow::Result<Range<usize>> {
        let n = self.length()?;
        let start = self.at;
        self.take(n)?;
        Ok(start..self.at)
    }
}

/// Parse `MAGIC || EncryptedObject`; ranges are relative to `blob`.
pub fn parse(blob: &[u8]) -> anyhow::Result<Object> {
    anyhow::ensure!(is_envelope(blob), "not a SuiFleet Seal envelope");
    let mut r = Reader { bytes: blob, at: MAGIC.len() };
    anyhow::ensure!(r.byte()? == 0, "unsupported Seal object version");
    let package_id = r.array::<32>()?;
    let id = r.vector()?;
    let id = blob[id].to_vec();
    let count = r.length()?;
    anyhow::ensure!((1..255).contains(&count), "invalid Seal key server count");
    let mut services = Vec::with_capacity(count);
    for _ in 0..count {
        services.push((r.array::<32>()?, r.byte()?));
    }
    let threshold = r.byte()?;
    anyhow::ensure!(threshold >= 1 && usize::from(threshold) <= count, "invalid Seal threshold");
    anyhow::ensure!(r.length()? == 0, "unsupported Seal KEM");
    let nonce_bytes = r.array::<96>()?;
    let nonce = G2Affine::deserialize_compressed(&nonce_bytes[..]).map_err(|_| anyhow::anyhow!("invalid Seal nonce"))?;
    let shares = r.length()?;
    anyhow::ensure!(shares == count, "Seal share count differs from its key servers");
    let encrypted_shares = (0..shares).map(|_| r.array::<32>()).collect::<anyhow::Result<Vec<_>>>()?;
    let encrypted_randomness = r.array::<32>()?;
    let kind = r.length()?;
    anyhow::ensure!(kind <= 1, "unsupported Seal ciphertext (plain or unknown)");
    let blob_range = r.vector()?;
    let aad = match r.byte()? {
        0 => Vec::new(),
        1 => blob[r.vector()?].to_vec(),
        _ => anyhow::bail!("invalid Seal AAD option"),
    };
    let dem = if kind == 0 { Dem::Aes256Gcm } else { Dem::Hmac256Ctr { mac: r.array::<32>()? } };
    anyhow::ensure!(r.at == blob.len(), "trailing bytes after the Seal object");
    let indices: std::collections::BTreeSet<u8> = services.iter().map(|(_, i)| *i).collect();
    anyhow::ensure!(indices.len() == count && !indices.contains(&0), "invalid Seal share indices");
    Ok(Object {
        package_id,
        id,
        services,
        threshold,
        nonce,
        nonce_bytes,
        encrypted_shares,
        encrypted_randomness,
        dem,
        aad,
        blob: blob_range,
    })
}

// ── BLS12-381 ────────────────────────────────────────────────────────────────

pub fn hash_to_g1(message: &[u8]) -> G1Projective {
    MapToCurveBasedHasher::<G1Projective, DefaultFieldHasher<sha2::Sha256, 128>, WBMap<ark_bls12_381::g1::Config>>::new(BLS_DST)
        .and_then(|hasher| hasher.hash(message))
        .expect("hash to G1 is total")
        .into()
}

/// Seal's identity hash: the BLS hash of `IBE_DST || id`.
pub fn hash_identity(full_id: &[u8]) -> G1Projective {
    hash_to_g1(&[IBE_DST, full_id].concat())
}

pub fn g1_bytes(point: &G1Projective) -> [u8; 48] {
    let mut out = Vec::with_capacity(48);
    point.into_affine().serialize_compressed(&mut out).expect("G1 encoding");
    out.try_into().expect("48-byte G1 encoding")
}

pub fn g2_bytes(point: &G2Projective) -> [u8; 96] {
    let mut out = Vec::with_capacity(96);
    point.into_affine().serialize_compressed(&mut out).expect("G2 encoding");
    out.try_into().expect("96-byte G2 encoding")
}

/// A compressed point, rejected unless on the curve and in the subgroup.
pub fn g1(bytes: &[u8]) -> anyhow::Result<G1Affine> {
    anyhow::ensure!(bytes.len() == 48, "invalid G1 point length");
    G1Affine::deserialize_compressed(bytes).map_err(|_| anyhow::anyhow!("invalid G1 point"))
}

pub fn g2(bytes: &[u8]) -> anyhow::Result<G2Affine> {
    anyhow::ensure!(bytes.len() == 96, "invalid G2 point length");
    G2Affine::deserialize_compressed(bytes).map_err(|_| anyhow::anyhow!("invalid G2 point"))
}

/// A canonical big-endian scalar.
pub fn scalar(bytes: &[u8]) -> anyhow::Result<Fr> {
    anyhow::ensure!(bytes.len() == 32, "invalid scalar length");
    let value = Fr::from_be_bytes_mod_order(bytes);
    anyhow::ensure!(value.into_bigint().to_bytes_be() == bytes, "scalar out of range");
    Ok(value)
}

pub fn scalar_bytes(value: &Fr) -> [u8; 32] {
    value.into_bigint().to_bytes_be().try_into().expect("32 bytes")
}

pub fn random_scalar() -> Fr {
    use rand::RngCore;
    let mut bytes = zeroize::Zeroizing::new([0u8; 64]);
    rand::rngs::OsRng.fill_bytes(bytes.as_mut());
    Fr::from_le_bytes_mod_order(bytes.as_ref())
}

fn fq(value: &Fq) -> Vec<u8> {
    value.into_bigint().to_bytes_be()
}

/// The 576-byte encoding Seal's KDF hashes (fastcrypto's GT order).
pub fn gt_bytes(value: &Fq12) -> Vec<u8> {
    let (a, b) = (&value.c0, &value.c1);
    [&a.c0, &b.c0, &a.c1, &b.c1, &a.c2, &b.c2].iter().flat_map(|pair| [fq(&pair.c0), fq(&pair.c1)]).flatten().collect()
}

fn pairing(a: impl Into<G1Affine>, b: impl Into<G2Affine>) -> Fq12 {
    Bls12_381::pairing(a.into(), b.into()).0
}

/// e(a, b) == e(c, d).
fn same_pairing(a: G1Affine, b: G2Affine, c: G1Affine, d: G2Affine) -> bool {
    Bls12_381::multi_pairing([a, (-c.into_group()).into_affine()], [b, d]) == PairingOutput::zero()
}

/// A key server's user secret key for `full_id` is `master * H(id)`.
pub fn verify_user_secret_key(key: &G1Affine, full_id: &[u8], public_key: &G2Affine) -> bool {
    same_pairing(*key, G2Affine::generator(), hash_identity(full_id).into_affine(), *public_key)
}

/// Seal's proof of possession: a BLS signature by the master key over
/// `POP_DST || public key || key server object ID`.
pub fn verify_proof_of_possession(signature: &[u8], public_key: &G2Affine, object_id: &[u8; 32]) -> bool {
    let Ok(signature) = g1(signature) else { return false };
    let mut message = POP_DST.to_vec();
    message.extend_from_slice(&g2_bytes(&public_key.into_group()));
    message.extend_from_slice(object_id);
    same_pairing(signature, G2Affine::generator(), hash_to_g1(&message).into_affine(), *public_key)
}

/// ElGamal in G1 (how key servers return user secret keys): `c1 - sk * c0`.
pub fn elgamal_decrypt(secret: &Fr, c0: &G1Affine, c1: &G1Affine) -> G1Affine {
    (c1.into_group() - c0.into_group() * secret).into_affine()
}

/// Whether a request's ElGamal key and verification key share one secret.
pub fn elgamal_pair_valid(public_key: &[u8], verification_key: &[u8]) -> bool {
    match (g1(public_key), g2(verification_key)) {
        (Ok(public), Ok(verification)) => same_pairing(public, G2Affine::generator(), G1Affine::generator(), verification),
        _ => false,
    }
}

pub fn elgamal_keys(secret: &Fr) -> ([u8; 48], [u8; 96]) {
    (g1_bytes(&(G1Projective::generator() * secret)), g2_bytes(&(G2Projective::generator() * secret)))
}

// ── KDF, shares and DEM ──────────────────────────────────────────────────────

fn kdf(element: &Fq12, nonce: &[u8; 96], full_id: &[u8], object_id: &[u8; 32], index: u8) -> [u8; 32] {
    let mut hash = Sha3_256::new();
    hash.update(KDF_DST);
    hash.update(gt_bytes(element));
    hash.update(nonce);
    hash.update(g1_bytes(&hash_identity(full_id)));
    hash.update(object_id);
    hash.update([index]);
    hash.finalize().into()
}

#[derive(Clone, Copy)]
enum Purpose {
    Randomness = 0,
    Dem = 1,
}

fn derive_key(purpose: Purpose, base: &[u8; 32], object: &Object) -> [u8; 32] {
    let mut hash = Sha3_256::new();
    hash.update(DERIVE_KEY_DST);
    hash.update(base);
    hash.update([purpose as u8, object.threshold]);
    for share in &object.encrypted_shares {
        hash.update(share);
    }
    for (id, _) in &object.services {
        hash.update(id);
    }
    hash.finalize().into()
}

fn xor(a: &[u8; 32], b: &[u8; 32]) -> [u8; 32] {
    std::array::from_fn(|i| a[i] ^ b[i])
}

/// GF(2^8) with the AES polynomial, as Seal's Shamir sharing uses.
fn gf_mul(mut a: u8, mut b: u8) -> u8 {
    let mut product = 0;
    while b != 0 {
        if b & 1 != 0 {
            product ^= a;
        }
        let carry = a & 0x80 != 0;
        a <<= 1;
        if carry {
            a ^= 0x1b;
        }
        b >>= 1;
    }
    product
}

fn gf_inverse(a: u8) -> u8 {
    // a^254 = a^-1 for nonzero a.
    let mut result = 1;
    let mut base = a;
    let mut exponent = 254u8;
    while exponent != 0 {
        if exponent & 1 != 0 {
            result = gf_mul(result, base);
        }
        base = gf_mul(base, base);
        exponent >>= 1;
    }
    result
}

/// Lagrange interpolation of byte-wise shares, evaluated at `x`.
fn interpolate(shares: &[(u8, [u8; 32])], x: u8) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (j, (xj, yj)) in shares.iter().enumerate() {
        let mut basis = 1u8;
        for (i, (xi, _)) in shares.iter().enumerate() {
            if i != j {
                basis = gf_mul(basis, gf_mul(x ^ xi, gf_inverse(xj ^ xi)));
            }
        }
        for (byte, y) in out.iter_mut().zip(yj) {
            *byte ^= gf_mul(*y, basis);
        }
    }
    out
}

/// Recover the base key from at least `threshold` user secret keys (by key
/// server object ID), check the encryption's randomness and the consistency
/// of every share, and return the DEM key. `public_keys` must cover every
/// key server of the object.
pub fn dem_key(
    object: &Object,
    keys: &HashMap<[u8; 32], G1Affine>,
    public_keys: &HashMap<[u8; 32], G2Affine>,
) -> anyhow::Result<zeroize::Zeroizing<[u8; 32]>> {
    let full_id = object.full_id();
    let mut shares = Vec::new();
    for ((id, index), encrypted) in object.services.iter().zip(&object.encrypted_shares) {
        if let Some(key) = keys.get(id) {
            if shares.len() < usize::from(object.threshold) {
                let element = pairing(*key, object.nonce);
                shares.push((*index, xor(encrypted, &kdf(&element, &object.nonce_bytes, &full_id, id, *index))));
            }
        }
    }
    anyhow::ensure!(shares.len() >= usize::from(object.threshold), "only {} of {} required key shares", shares.len(), object.threshold);
    let base = zeroize::Zeroizing::new(interpolate(&shares, 0));
    let randomness = xor(&object.encrypted_randomness, &derive_key(Purpose::Randomness, &base, object));
    let r = scalar(&randomness).map_err(|_| anyhow::anyhow!("invalid Seal randomness"))?;
    anyhow::ensure!((G2Projective::generator() * r).into_affine() == object.nonce, "Seal nonce does not match its randomness");
    // Every share, not just the ones used, must lie on the same polynomial:
    // otherwise different key server subsets could yield different plaintexts.
    let gid_r = (hash_identity(&full_id) * r).into_affine();
    for ((id, index), encrypted) in object.services.iter().zip(&object.encrypted_shares) {
        let public_key = public_keys.get(id).ok_or_else(|| anyhow::anyhow!("no public key for key server 0x{}", hex::encode(id)))?;
        let share = xor(encrypted, &kdf(&pairing(gid_r, *public_key), &object.nonce_bytes, &full_id, id, *index));
        anyhow::ensure!(share == interpolate(&shares, *index), "inconsistent Seal key shares");
    }
    Ok(zeroize::Zeroizing::new(derive_key(Purpose::Dem, &base, object)))
}

type HmacSha3 = hmac::Hmac<Sha3_256>;

fn hmac(key: &[u8; 32], parts: &[&[u8]]) -> [u8; 32] {
    use hmac::Mac;
    let mut mac = <HmacSha3 as Mac>::new_from_slice(key).expect("any key length");
    for part in parts {
        mac.update(part);
    }
    mac.finalize().into_bytes().into()
}

/// Decrypt the object's ciphertext inside `buffer` (the parsed envelope) in
/// place; returns where the plaintext now is. Authenticates first.
pub fn decrypt_in_place(object: &Object, key: &[u8; 32], buffer: &mut [u8]) -> anyhow::Result<Range<usize>> {
    let blob = object.blob.clone();
    match &object.dem {
        Dem::Aes256Gcm => {
            use aes_gcm::{aead::AeadInPlace, aes::Aes256, AesGcm, KeyInit};
            anyhow::ensure!(blob.len() >= 16, "truncated AES-GCM ciphertext");
            let split = blob.end - 16;
            let tag = aes_gcm::Tag::clone_from_slice(&buffer[split..blob.end]);
            let cipher = AesGcm::<Aes256, aes_gcm::aead::consts::U16>::new_from_slice(key).expect("32-byte key");
            cipher
                .decrypt_in_place_detached(&AES_IV.into(), &object.aad, &mut buffer[blob.start..split], &tag)
                .map_err(|_| anyhow::anyhow!("Seal ciphertext failed authentication"))?;
            Ok(blob.start..split)
        }
        Dem::Hmac256Ctr { mac } => {
            let ciphertext = &buffer[blob.clone()];
            let expected = hmac(key, &[b"HMAC-CTR-MAC", &(object.aad.len() as u64).to_le_bytes(), &object.aad, ciphertext]);
            use subtle_eq::ConstantTimeEq;
            anyhow::ensure!(expected.ct_eq(mac), "Seal ciphertext failed authentication");
            for (block, chunk) in buffer[blob.clone()].chunks_mut(32).enumerate() {
                let pad = hmac(key, &[b"HMAC-CTR-ENC", &(block as u64).to_le_bytes()]);
                chunk.iter_mut().zip(pad).for_each(|(byte, pad)| *byte ^= pad);
            }
            Ok(blob)
        }
    }
}

mod subtle_eq {
    pub trait ConstantTimeEq {
        fn ct_eq(&self, other: &Self) -> bool;
    }
    impl ConstantTimeEq for [u8; 32] {
        fn ct_eq(&self, other: &Self) -> bool {
            self.iter().zip(other).fold(0u8, |acc, (a, b)| acc | (a ^ b)) == 0
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use serde_json::Value;

    pub fn vectors() -> Value {
        serde_json::from_str(include_str!("../../interfaces/seal-vectors.json")).unwrap()
    }
    pub fn unhex(value: &Value) -> Vec<u8> {
        hex::decode(value.as_str().unwrap().trim_start_matches("0x")).unwrap()
    }
    fn id32(value: &Value) -> [u8; 32] {
        unhex(value).try_into().unwrap()
    }

    /// Master secrets, user keys and public keys of the vectors' servers.
    pub fn servers(v: &Value) -> (HashMap<[u8; 32], G1Affine>, HashMap<[u8; 32], G2Affine>) {
        let mut keys = HashMap::new();
        let mut public = HashMap::new();
        for server in v["servers"].as_array().unwrap() {
            let id = id32(&server["object_id"]);
            keys.insert(id, g1(&unhex(&server["user_secret_key"])).unwrap());
            public.insert(id, g2(&unhex(&server["public_key"])).unwrap());
        }
        (keys, public)
    }

    pub fn envelope(object: &[u8]) -> Vec<u8> {
        [MAGIC, object].concat()
    }

    #[test]
    fn matches_the_sdks_group_encodings() {
        let v = vectors();
        let full_id = unhex(&v["full_id"]);
        assert_eq!(g1_bytes(&hash_identity(&full_id)).to_vec(), unhex(&v["hash_to_g1"]));
        let a = scalar(&unhex(&v["gt"]["g1_scalar"])).unwrap();
        let b = scalar(&unhex(&v["gt"]["g2_scalar"])).unwrap();
        let gt = pairing(G1Projective::generator() * a, G2Projective::generator() * b);
        assert_eq!(gt_bytes(&gt), unhex(&v["gt"]["bytes"]));
        for server in v["servers"].as_array().unwrap() {
            let master = scalar(&unhex(&server["master_secret"])).unwrap();
            assert_eq!(g2_bytes(&(G2Projective::generator() * master)).to_vec(), unhex(&server["public_key"]));
            let key = hash_identity(&full_id) * master;
            assert_eq!(g1_bytes(&key).to_vec(), unhex(&server["user_secret_key"]));
            let public = g2(&unhex(&server["public_key"])).unwrap();
            assert!(verify_user_secret_key(&key.into_affine(), &full_id, &public));
            assert!(!verify_user_secret_key(&key.into_affine(), &full_id[1..], &public), "another identity's key");
        }
    }

    #[test]
    fn decrypts_sdk_ciphertexts_with_any_threshold_subset() {
        let v = vectors();
        let (keys, public) = servers(&v);
        let plaintext = unhex(&v["plaintext"]);
        for case in v["encrypted"].as_array().unwrap() {
            let bytes = envelope(&unhex(&case["encrypted_object"]));
            let object = parse(&bytes).unwrap();
            assert_eq!(object.package_id.to_vec(), unhex(&v["package_id"]));
            assert_eq!(object.id, unhex(&v["inner_id"]));
            assert_eq!(object.aad, unhex(&v["aad"]));
            assert_eq!(object.threshold, 2);
            assert_eq!(object.key_epoch(), Some(3));
            for skip in object.services.iter().map(|(id, _)| *id) {
                let subset: HashMap<_, _> = keys.iter().filter(|(id, _)| **id != skip).map(|(k, v)| (*k, *v)).collect();
                let key = dem_key(&object, &subset, &public).unwrap();
                assert_eq!(key.to_vec(), unhex(&case["dem_key"]), "{}", case["dem"]);
                let mut buffer = bytes.clone();
                let range = decrypt_in_place(&object, &key, &mut buffer).unwrap();
                assert_eq!(buffer[range].to_vec(), plaintext, "{}", case["dem"]);
            }
            let one: HashMap<_, _> = keys.iter().take(1).map(|(k, v)| (*k, *v)).collect();
            assert!(dem_key(&object, &one, &public).unwrap_err().to_string().contains("required key shares"));
        }
    }

    #[test]
    fn rejects_tampering_and_wrong_keys() {
        let v = vectors();
        let (keys, public) = servers(&v);
        for case in v["encrypted"].as_array().unwrap() {
            let bytes = envelope(&unhex(&case["encrypted_object"]));
            let object = parse(&bytes).unwrap();
            let key = dem_key(&object, &keys, &public).unwrap();
            // A flipped ciphertext byte fails authentication.
            let mut tampered = bytes.clone();
            tampered[object.blob.start + 3] ^= 1;
            assert!(decrypt_in_place(&object, &key, &mut tampered).is_err(), "{}", case["dem"]);
            // Changed associated data fails authentication.
            let mut other = object.clone();
            other.aad = aad(Kind::Recipe, "00");
            assert!(decrypt_in_place(&other, &key, &mut bytes.clone()).is_err());
            // Another identity's keys (e.g. a rotated epoch) do not decrypt.
            let mut moved = object.clone();
            moved.id[32] ^= 1;
            assert!(dem_key(&moved, &keys, &public).is_err());
            // A tampered encrypted share, even one not used for decryption, is
            // rejected (it is bound into the randomness key and share check).
            let mut shares = object.clone();
            shares.encrypted_shares[2][0] ^= 1;
            let subset: HashMap<_, _> = keys.iter().filter(|(id, _)| **id != shares.services[2].0).map(|(k, v)| (*k, *v)).collect();
            assert!(dem_key(&shares, &subset, &public).is_err());
            // Every server's public key is needed for that check.
            let mut partial = public.clone();
            partial.remove(&object.services[0].0);
            assert!(dem_key(&object, &keys, &partial).is_err());
        }
    }

    #[test]
    fn parsing_is_strict() {
        let v = vectors();
        let object = unhex(&v["encrypted"][0]["encrypted_object"]);
        assert!(parse(&object).is_err(), "no magic");
        let bytes = envelope(&object);
        let mut trailing = bytes.clone();
        trailing.push(0);
        assert!(parse(&trailing).is_err());
        for cut in [MAGIC.len(), MAGIC.len() + 1, MAGIC.len() + 40, bytes.len() - 1] {
            assert!(parse(&bytes[..cut]).is_err(), "truncated at {cut}");
        }
        let mut version = bytes.clone();
        version[MAGIC.len()] = 1;
        assert!(parse(&version).is_err());
    }

    #[test]
    fn elgamal_and_proof_of_possession_match_the_sdk() {
        let v = vectors();
        let e = &v["elgamal"];
        let secret = scalar(&unhex(&e["secret"])).unwrap();
        let (public, verification) = elgamal_keys(&secret);
        assert!(elgamal_pair_valid(&public, &verification));
        assert!(!elgamal_pair_valid(&public, &elgamal_keys(&random_scalar()).1));
        assert_eq!(public.to_vec(), unhex(&e["public_key"]));
        assert_eq!(verification.to_vec(), unhex(&e["verification_key"]));
        let c = e["ciphertext"].as_array().unwrap();
        let key = elgamal_decrypt(&secret, &g1(&unhex(&c[0])).unwrap(), &g1(&unhex(&c[1])).unwrap());
        assert_eq!(g1_bytes(&key.into_group()).to_vec(), unhex(&e["plaintext"]));
        let pop = &v["proof_of_possession"];
        let public = g2(&unhex(&pop["public_key"])).unwrap();
        let id = id32(&pop["object_id"]);
        assert!(verify_proof_of_possession(&unhex(&pop["signature"]), &public, &id));
        let mut other = id;
        other[0] ^= 1;
        assert!(!verify_proof_of_possession(&unhex(&pop["signature"]), &public, &other));
    }

    #[test]
    fn shamir_and_scalars() {
        assert_eq!(gf_mul(3, 0x55), 0xff);
        assert_eq!(gf_mul(3, 0xff), 26);
        for a in 1..=255u8 {
            assert_eq!(gf_mul(a, gf_inverse(a)), 1);
        }
        let order = Fr::MODULUS.to_bytes_be();
        assert!(scalar(&order).is_err(), "the group order is out of range");
        assert!(scalar(&[0u8; 31]).is_err());
        let r = random_scalar();
        assert_eq!(scalar(&scalar_bytes(&r)).unwrap(), r);
    }
}

/// Encrypt with public on-chain server keys. No session or device read grant is
/// needed to write confidential data. The caller supplies the fleet policy ID
/// and signed archive AAD; encryption itself does not authenticate the writer.
pub fn encrypt(package_id: [u8; 32], id: Vec<u8>, servers: &[([u8; 32], G2Affine)],
    threshold: u8, aad: Vec<u8>, plaintext: &[u8]) -> anyhow::Result<Vec<u8>> {
    use rand::RngCore;
    use aes_gcm::{aead::AeadInPlace, aes::Aes256, AesGcm, KeyInit};
    anyhow::ensure!(!servers.is_empty() && servers.len() < 255 && threshold > 0 && usize::from(threshold) <= servers.len(), "invalid encryption threshold");
    let unique: std::collections::BTreeSet<_> = servers.iter().map(|s| s.0).collect();
    anyhow::ensure!(unique.len() == servers.len() && servers.iter().all(|(_, pk)| !pk.is_zero()), "duplicate/invalid encryption server");
    let randomness = random_scalar();
    let nonce = (G2Projective::generator() * randomness).into_affine();
    let mut object = Object { package_id, id, services: servers.iter().enumerate().map(|(i,s)| (s.0, (i+1) as u8)).collect(),
        threshold, nonce, nonce_bytes: g2_bytes(&nonce.into_group()), encrypted_shares: vec![], encrypted_randomness: [0;32],
        dem: Dem::Aes256Gcm, aad, blob: 0..0 };
    let mut polynomial = zeroize::Zeroizing::new(vec![[0u8;32]; threshold as usize]);
    for coefficient in polynomial.iter_mut() { rand::rngs::OsRng.fill_bytes(coefficient); }
    let full_id = object.full_id();
    let gid_r = (hash_identity(&full_id) * randomness).into_affine();
    for ((server_id, pk), (_, index)) in servers.iter().zip(&object.services) {
        let share: [u8;32] = std::array::from_fn(|b| polynomial.iter().rev().fold(0, |acc,c| gf_mul(acc,*index)^c[b]));
        object.encrypted_shares.push(xor(&share, &kdf(&pairing(gid_r,*pk), &object.nonce_bytes, &full_id, server_id, *index)));
    }
    object.encrypted_randomness = xor(&scalar_bytes(&randomness), &derive_key(Purpose::Randomness, &polynomial[0], &object));
    let key = zeroize::Zeroizing::new(derive_key(Purpose::Dem, &polynomial[0], &object));
    let mut ciphertext = plaintext.to_vec();
    let cipher = AesGcm::<Aes256, aes_gcm::aead::consts::U16>::new_from_slice(key.as_slice()).expect("32-byte key");
    let tag = cipher.encrypt_in_place_detached(&AES_IV.into(), &object.aad, &mut ciphertext).map_err(|_| anyhow::anyhow!("archive encryption failed"))?;
    ciphertext.extend_from_slice(&tag);
    let mut out = MAGIC.to_vec();
    out.push(0); out.extend(package_id); out.extend(bcs::to_bytes(&object.id)?);
    out.extend(bcs::to_bytes(&object.services)?); out.push(threshold); out.push(0);
    out.extend(object.nonce_bytes); out.extend(bcs::to_bytes(&object.encrypted_shares)?);
    out.extend(object.encrypted_randomness); out.push(0); out.extend(bcs::to_bytes(&ciphertext)?);
    out.extend(bcs::to_bytes(&Some(object.aad))?);
    Ok(out)
}
