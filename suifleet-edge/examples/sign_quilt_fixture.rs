//! Testnet-only Quilt fixture signing bridge. Not installed with the daemon.
//! Signs only bounded synthetic archive bindings or a five-minute Seal session;
//! no keys or transaction signatures leave the explicitly selected keystore.
use base64::{engine::general_purpose::STANDARD, Engine};
use blake2::{Blake2b, Digest};
use ed25519_dalek::{Signer, SigningKey};
use serde::Deserialize;
use std::{io::{Read, Write}, path::PathBuf, str::FromStr};
use sui_sdk_types::Address;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request { wallet: PathBuf, keystore: PathBuf, expected_address: String, namespace: String, message: String, fleet: String, device: String }
fn hash(bytes: &[u8]) -> Vec<u8> { Blake2b::<digest::consts::U32>::digest(bytes).to_vec() }
fn main() -> anyhow::Result<()> {
    let mut bytes=zeroize::Zeroizing::new(Vec::new());std::io::stdin().take(32769).read_to_end(&mut bytes)?;
    anyhow::ensure!(bytes.len()<=32768,"session request too large");
    let r:Request=serde_json::from_slice(&bytes)?;
    anyhow::ensure!(r.wallet.is_file() && r.keystore.is_file(),"explicit wallet profile/keystore missing");
    let namespace=Address::from_str(&r.namespace)?.to_string();
    let message=STANDARD.decode(r.message)?;
    let text=std::str::from_utf8(&message)?;
    anyhow::ensure!(message.len()<16384, "fixture message too large");
    if text.starts_with('{') {
        let a: serde_json::Value = serde_json::from_slice(&message)?;
        anyhow::ensure!(matches!(a["kind"].as_str(),Some("fleet-data"|"fleet-opaque")) && a["chain_identifier"] == "4c78adac"
            && matches!(a["schema"].as_u64(),Some(1|4))
            && a["fleet_id"].as_str().and_then(|v|Address::from_str(v).ok()) == Some(Address::from_str(&r.fleet)?)
            && a["device_id"].as_str().and_then(|v|Address::from_str(v).ok()) == Some(Address::from_str(&r.device)?)
            && a["signer"].as_str().and_then(|v|Address::from_str(v).ok()) == Some(Address::from_str(&r.expected_address)?), "foreign fixture binding");
        if a["kind"] == "fleet-opaque" {
            anyhow::ensure!(a["schema"]==1,"unsupported opaque fixture schema");
            let data=STANDARD.decode(a["item"]["data"].as_str().ok_or_else(||anyhow::anyhow!("missing synthetic data"))?)?;
            anyhow::ensure!(data == b"public synthetic opaque text\n" || data == [0,255,128,0,10], "only synthetic opaque fixture bytes allowed");
        } else {
        let records=a["records"].as_array().ok_or_else(||anyhow::anyhow!("missing records"))?;
        anyhow::ensure!(records.len()<=2 && records.iter().all(|v|v["line"].as_str().is_some_and(|s|s.starts_with("public synthetic Quilt item ") && s.len()<100)), "only synthetic fixture records allowed");
        }
    } else {
        anyhow::ensure!(text.starts_with(&format!("Accessing keys of package {namespace} for 5 mins from ")), "not selected five-minute Seal session");
        let (_,session)=text.rsplit_once(", session key ").ok_or_else(||anyhow::anyhow!("missing session public key"))?;
        anyhow::ensure!(STANDARD.decode(session)?.len()==32,"invalid session key");
    }
    let raw=zeroize::Zeroizing::new(std::fs::read(r.keystore)?);
    let keys=zeroize::Zeroizing::new(serde_json::from_slice::<Vec<String>>(&raw)?);
    let expected=Address::from_str(&r.expected_address)?;
    for encoded in keys.iter() {
        let raw=zeroize::Zeroizing::new(STANDARD.decode(encoded)?);
        if raw.len()!=33 || raw[0]!=0 { continue; }
        let key=SigningKey::from_bytes(raw[1..].try_into()?);
        let public=key.verifying_key().to_bytes();
        if hash(&[&[0u8][..],public.as_slice()].concat())!=expected.inner(){continue;}
        let mut intent=vec![3,0,0];intent.extend(bcs::to_bytes(&message)?);
        let sig=key.sign(&hash(&intent));
        let signature=[&[0u8][..],sig.to_bytes().as_slice(),public.as_slice()].concat();
        writeln!(std::io::stdout(),"{}",serde_json::json!({"signature":STANDARD.encode(signature)}))?;
        return Ok(());
    }
    anyhow::bail!("selected wallet does not contain the expected Ed25519 account")
}
