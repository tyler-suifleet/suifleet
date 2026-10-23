//! Public interoperability fixture only. It uses the existing test vector's
//! public provider keys and identity, never a runtime signing credential.
#[path = "../src/seal.rs"]
#[allow(dead_code)]
mod seal;
fn main() -> anyhow::Result<()> {
    let v: serde_json::Value = serde_json::from_str(include_str!("../../interfaces/seal-vectors.json"))?;
    let package: [u8;32] = hex::decode(v["package_id"].as_str().unwrap().trim_start_matches("0x"))?.try_into().unwrap();
    let id = hex::decode(v["inner_id"].as_str().unwrap().trim_start_matches("0x"))?;
    let mut servers = Vec::new();
    for server in v["servers"].as_array().unwrap() { servers.push((hex::decode(server["object_id"].as_str().unwrap().trim_start_matches("0x"))?.try_into().unwrap(),seal::g2(&hex::decode(server["public_key"].as_str().unwrap())?)?)); }
    let encrypted = seal::encrypt(package,id,&servers,2,b"native-sdk-interoperability".to_vec(),b"public native encryption test fixture")?;
    println!("{}",hex::encode(&encrypted[seal::MAGIC.len()..])); Ok(())
}
