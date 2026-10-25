//! Opaque component data. A queued response is custody, never storage acknowledgement.
//! One locked, bounded journal; publication and retirement belong to fleet_archive.
use std::{path::{Path, PathBuf}, fs::OpenOptions, os::unix::fs::OpenOptionsExt};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde::{Serialize, Deserialize};
use sha2::{Digest, Sha256};
pub const CHUNK: usize = 24 * 1024;
pub const MAX_FILE: u64 = 64 * 1024 * 1024;
pub const MAX_QUEUE: u64 = 64 * 1024 * 1024;
pub const MAX_ITEMS: usize = 4096;
fn full<T>() -> anyhow::Result<T> {
    Err(crate::streams::StreamError {code:"full",oldest:None,detail:"opaque spool full; retry after verified publication".into()}.into())
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Metadata {
    pub stream: String, pub key: String, pub content_type: String,
    #[serde(default)] pub metadata: std::collections::BTreeMap<String,String>,
    pub upload: String, pub chunk: u32, pub chunks: u32, pub total_size: u64,
    pub size: u64, pub sha256: Vec<u8>,
}
impl Metadata {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(crate::streams::valid_name(&self.stream) && self.stream.len() <= 128 && !self.key.is_empty() && self.key.len() <= 256
            && !self.content_type.is_empty() && self.content_type.len() <= 128
            && self.upload.len() == 64 && self.upload.bytes().all(|b| b.is_ascii_hexdigit())
            && self.chunks > 0 && self.chunks <= 2731 && self.chunk < self.chunks
            && self.total_size <= MAX_FILE && self.size <= CHUNK as u64 && self.size <= self.total_size
            && self.sha256.len() == 32 && self.metadata.len() <= 16
            && self.metadata.iter().all(|(k,v)| !k.is_empty() && k.len() <= 64 && v.len() <= 256), "invalid opaque item metadata");
        anyhow::ensure!(serde_json::to_vec(&self.metadata)?.len() <= 6144, "opaque metadata byte limit");
        let chunks=self.total_size.max(1).div_ceil(CHUNK as u64);
        let expected=self.total_size.saturating_sub(self.chunk as u64*CHUNK as u64).min(CHUNK as u64);
        anyhow::ensure!(self.chunks as u64 == chunks && self.size == expected, "invalid opaque chunk geometry");
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Item { pub sequence: u64, pub descriptor: Metadata, pub data: String,
    #[serde(default)] pub source_sequence: Option<u64> }
impl Item {
    pub fn bytes(&self) -> anyhow::Result<Vec<u8>> {
        self.descriptor.validate()?;
        let bytes = STANDARD.decode(&self.data)?;
        anyhow::ensure!(bytes.len() as u64 == self.descriptor.size && Sha256::digest(&bytes).as_slice() == self.descriptor.sha256, "opaque bytes changed");
        Ok(bytes)
    }
}
// Payload files are immutable. Only this compact custody index is replaced on intake.
#[derive(Clone, Serialize, Deserialize)]
struct Entry { sequence: u64, stream: String, upload: String, chunk: u32, length: u64 }
#[derive(Default, Serialize, Deserialize)]
struct Journal { next: u64, entries: Vec<Entry> }
pub struct Queue { dir: PathBuf, _lock: std::fs::File, journal: Journal, items: Vec<Item> }
impl Queue {
    pub fn open(state_dir: &Path) -> anyhow::Result<Self> { Self::load(state_dir, true) }
    fn load(state_dir: &Path, payloads: bool) -> anyhow::Result<Self> {
        let dir = state_dir.join("fleet-data/opaque"); std::fs::create_dir_all(&dir)?;
        let lock = OpenOptions::new().read(true).write(true).create(true).truncate(false).mode(0o600).open(dir.join("lock"))?;
        fs2::FileExt::lock_exclusive(&lock)?;
        let journal: Journal = match std::fs::read(dir.join("index.json")) {
            Ok(b) => { anyhow::ensure!(b.len() <= 2 * 1024 * 1024, "opaque index bound"); serde_json::from_slice(&b)? },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Journal::default(), Err(e) => return Err(e.into()) };
        let mut queue=Self {dir, _lock:lock, journal, items:Vec::new()};
        // One-time migration: commit the new index before removing legacy custody.
        if !queue.dir.join("index.json").exists() && queue.dir.join("queue.json").exists() {
            #[derive(Deserialize)] struct Legacy {next:u64, items:Vec<Item>}
            let b=std::fs::read(queue.dir.join("queue.json"))?;
            anyhow::ensure!(b.len() <= MAX_QUEUE as usize, "opaque legacy queue bound");
            let legacy:Legacy=serde_json::from_slice(&b)?;
            anyhow::ensure!(legacy.items.len() <= MAX_ITEMS
                && legacy.items.windows(2).all(|p|p[0].sequence < p[1].sequence)
                && legacy.items.last().is_none_or(|i|i.sequence < legacy.next), "opaque legacy sequence bound");
            for item in legacy.items { item.bytes()?; queue.persist_item(&item)?; }
            queue.journal.next=legacy.next; queue.save_index()?;
        }
        anyhow::ensure!(queue.journal.entries.len() <= MAX_ITEMS
            && queue.journal.entries.windows(2).all(|p|p[0].sequence < p[1].sequence)
            && queue.journal.entries.last().is_none_or(|i|i.sequence < queue.journal.next)
            && queue.journal.entries.iter().all(|e|e.length <= 65536)
            && queue.journal.entries.iter().map(|e|e.length).sum::<u64>() <= MAX_QUEUE,
            "invalid opaque custody index");
        // An interrupted put or retirement can leave unreferenced files. Never read
        // their payloads; reclaim them under the same exclusive lock.
        let live=queue.journal.entries.iter().map(|e|e.sequence).collect::<std::collections::HashSet<_>>();
        for entry in std::fs::read_dir(&queue.dir)? {
            let entry=entry?; let name=entry.file_name(); let name=name.to_string_lossy();
            if let Some(seq)=name.strip_suffix(".item").and_then(|n|n.parse::<u64>().ok()) {
                if !live.contains(&seq) { std::fs::remove_file(entry.path())?; }
            }
        }
        if queue.dir.join("index.json").exists() && queue.dir.join("queue.json").exists() {
            std::fs::remove_file(queue.dir.join("queue.json"))?;
        }
        if payloads {
            for entry in &queue.journal.entries { queue.items.push(queue.read_item(entry)?); }
        }
        Ok(queue)
    }
    fn read_item(&self, entry: &Entry) -> anyhow::Result<Item> {
        let path=self.dir.join(format!("{}.item",entry.sequence));
        anyhow::ensure!(std::fs::metadata(&path)?.len() == entry.length && entry.length <= 65536, "opaque item file bound");
        let item:Item=serde_json::from_slice(&std::fs::read(path)?)?;
        anyhow::ensure!(item.sequence == entry.sequence && item.descriptor.stream == entry.stream
            && item.descriptor.upload == entry.upload && item.descriptor.chunk == entry.chunk, "opaque index mismatch");
        item.bytes()?; Ok(item)
    }
    fn save_index(&self) -> anyhow::Result<()> {
        crate::fleet_archive::atomic(&self.dir.join("index.json"), &serde_json::to_vec(&self.journal)?)
    }
    fn persist_item(&mut self, item: &Item) -> anyhow::Result<()> {
        let bytes=serde_json::to_vec(item)?;
        if self.journal.entries.len() >= MAX_ITEMS || self.journal.entries.iter().map(|e|e.length).sum::<u64>() + bytes.len() as u64 > MAX_QUEUE { return full(); }
        crate::fleet_archive::atomic(&self.dir.join(format!("{}.item",item.sequence)), &bytes)?;
        self.journal.entries.push(Entry {sequence:item.sequence,stream:item.descriptor.stream.clone(),upload:item.descriptor.upload.clone(),chunk:item.descriptor.chunk,length:bytes.len() as u64});
        Ok(())
    }
    pub fn items(&self) -> &[Item] { &self.items }
    pub fn submit(&mut self, descriptor: Metadata, bytes: &[u8], source_sequence: Option<u64>) -> anyhow::Result<u64> {
        descriptor.validate()?;
        anyhow::ensure!(bytes.len() as u64 == descriptor.size && Sha256::digest(bytes).as_slice() == descriptor.sha256, "opaque content digest/length mismatch");
        if let Some(entry) = self.journal.entries.iter().find(|i| i.stream == descriptor.stream && i.upload == descriptor.upload && i.chunk == descriptor.chunk) {
            let old=self.read_item(entry)?;
            anyhow::ensure!(old.descriptor == descriptor && old.data == STANDARD.encode(bytes) && old.source_sequence == source_sequence, "conflicting retry");
            return Ok(old.sequence);
        }
        if self.journal.next >= 9_007_199_254_740_991 { return full(); }
        let seq=self.journal.next;
        let item=Item {sequence:seq,descriptor,data:STANDARD.encode(bytes),source_sequence};
        self.persist_item(&item)?; self.journal.next+=1;
        if let Err(error)=self.save_index() {
            self.journal.entries.pop();self.journal.next=seq;return Err(error);
        }
        self.items.push(item); Ok(seq)
    }
    // Reserve and commit all chunks of one stream message together. A full
    // spool must still be allowed to publish older custody and free capacity.
    fn submit_message(&mut self, items: Vec<Item>) -> anyhow::Result<()> {
        let added=items.iter().map(|i|serde_json::to_vec(i).map(|b|b.len() as u64)).collect::<Result<Vec<_>,_>>()?.iter().sum::<u64>();
        if self.journal.entries.len()+items.len()>MAX_ITEMS
            || self.journal.entries.iter().map(|e|e.length).sum::<u64>()+added>MAX_QUEUE
            || self.journal.next+items.len() as u64>=9_007_199_254_740_991 {return full();}
        let previous=self.journal.entries.len();let next=self.journal.next;
        let result=(|| {
            for item in &items {item.bytes()?;self.persist_item(item)?;}
            self.journal.next+=items.len() as u64;self.save_index()
        })();
        if let Err(error)=result {self.journal.entries.truncate(previous);self.journal.next=next;return Err(error);}
        self.items.extend(items);Ok(())
    }
    /// Called only after independently read anchor AND item index verification.
    pub fn retire(&mut self, sequences: &[u64]) -> anyhow::Result<()> {
        let old=std::mem::take(&mut self.journal.entries);
        self.journal.entries=old.iter().filter(|e|!sequences.contains(&e.sequence)).cloned().collect();
        if let Err(error)=self.save_index() {self.journal.entries=old;return Err(error);}
        self.items.retain(|i|!sequences.contains(&i.sequence));
        for entry in old.iter().filter(|e|sequences.contains(&e.sequence)) {std::fs::remove_file(self.dir.join(format!("{}.item",entry.sequence)))?;}
        Ok(())
    }
}
pub fn handle(state_dir: &Path, stream: &str, payload: &serde_json::Value) -> anyhow::Result<serde_json::Value> {
    #[derive(Deserialize)] #[serde(deny_unknown_fields)]
    struct Put { descriptor: Metadata, data_base64: String }
    let put: Put = serde_json::from_value(payload.clone())?;
    anyhow::ensure!(put.descriptor.stream == stream, "opaque scope differs from authorized stream");
    let bytes = STANDARD.decode(put.data_base64)?;
    let seq = Queue::load(state_dir, false)?.submit(put.descriptor, &bytes, None)?;
    Ok(serde_json::json!({"sequence":seq,"state":"queued","storage_acknowledged":false}))
}

/// Signed plaintext inside each independently scoped ciphertext patch.
#[derive(Serialize, Deserialize)] #[serde(deny_unknown_fields)]
pub struct Message {
    pub kind: String, pub schema: u8, pub chain_identifier: String, pub registry: String,
    pub fleet_id: String, pub device_id: String, pub signer: String, pub batch_sequence: u64,
    pub item: Item,
}
impl Message {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.kind == "fleet-opaque" && self.schema == 1 && self.batch_sequence > 0 && self.batch_sequence <= 9_007_199_254_740_991, "invalid opaque envelope");
        for id in [&self.registry,&self.fleet_id,&self.device_id,&self.signer] { let _: sui_sdk_types::Address = id.parse()?; }
        anyhow::ensure!(self.chain_identifier.len() == 8, "invalid opaque chain");
        self.item.bytes()?; Ok(())
    }
}
#[cfg(test)] mod tests {
    use super::*;
    fn metadata(bytes: &[u8]) -> Metadata { Metadata {stream:"sensor".into(),key:"raw".into(),content_type:"application/octet-stream".into(),metadata:Default::default(),upload:"ab".repeat(32),chunk:0,chunks:1,total_size:bytes.len() as u64,size:bytes.len() as u64,sha256:Sha256::digest(bytes).to_vec()} }
    #[test] fn raw_binary_retry_restart_and_retirement() {
        let dir=std::env::temp_dir().join(format!("sf139-{}",rand::random::<u128>()));
        let bytes=[0,255,128,10,0]; let m=metadata(&bytes);
        { let mut q=Queue::open(&dir).unwrap(); assert_eq!(q.submit(m.clone(),&bytes,None).unwrap(),0); assert_eq!(q.submit(m.clone(),&bytes,None).unwrap(),0); let mut bad=m.clone();bad.key="changed".into();assert!(q.submit(bad,&bytes,None).is_err()); }
        { let mut q=Queue::open(&dir).unwrap();assert_eq!(q.items()[0].bytes().unwrap(),bytes); q.retire(&[0]).unwrap(); }
        assert!(Queue::open(&dir).unwrap().items().is_empty());std::fs::remove_dir_all(dir).unwrap();
    }
    #[test] fn full_queue_rejects_without_losing_existing_custody() {
        let dir=std::env::temp_dir().join(format!("sf139-full-{}",rand::random::<u128>()));
        let mut q=Queue::open(&dir).unwrap();
        for n in 0..MAX_ITEMS { let mut m=metadata(&[0]);m.upload=format!("{n:064x}");q.submit(m,&[0],None).unwrap(); }
        let next=q.journal.next;
        let mut m=metadata(&[1]);m.upload="ff".repeat(32);
        let error=q.submit(m,&[1],None).unwrap_err();assert_eq!(error.downcast_ref::<crate::streams::StreamError>().unwrap().code,"full");
        assert_eq!(q.journal.next,next);drop(q);
        let q=Queue::open(&dir).unwrap();assert_eq!(q.items().len(),MAX_ITEMS);drop(q);std::fs::remove_dir_all(dir).unwrap();
    }
    #[test] fn intake_does_not_read_or_rewrite_existing_payloads() {
        use std::os::unix::fs::MetadataExt;
        let dir=std::env::temp_dir().join(format!("sf139-cost-{}",rand::random::<u128>()));
        let data=vec![7;CHUNK];
        for n in 0..64 {
            let mut m=metadata(&data);m.upload=format!("{n:064x}");
            Queue::load(&dir,false).unwrap().submit(m,&data,None).unwrap();
        }
        let root=dir.join("fleet-data/opaque");let first=root.join("0.item");
        let before=std::fs::metadata(&first).unwrap();
        // Unrelated corruption cannot cause intake to read/hash old payloads;
        // publication still refuses it when reading custody.
        std::fs::write(&first,b"corrupt").unwrap();
        let mut m=metadata(&data);m.upload="ff".repeat(32);
        let mut q=Queue::load(&dir,false).unwrap();q.submit(m,&data,None).unwrap();drop(q);
        let after=std::fs::metadata(&first).unwrap();
        assert_eq!(before.ino(),after.ino());assert_eq!(std::fs::read(&first).unwrap(),b"corrupt");
        assert!(std::fs::metadata(root.join("index.json")).unwrap().len()<16384);
        assert!(std::fs::metadata(root.join("64.item")).unwrap().len()<CHUNK as u64*2);
        assert!(Queue::open(&dir).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test] fn legacy_migration_and_interrupted_file_cleanup_preserve_custody() {
        let dir=std::env::temp_dir().join(format!("sf139-migrate-{}",rand::random::<u128>()));
        let root=dir.join("fleet-data/opaque");std::fs::create_dir_all(&root).unwrap();
        let item=Item {sequence:5,descriptor:metadata(&[1]),data:STANDARD.encode([1]),source_sequence:Some(9)};
        std::fs::write(root.join("queue.json"),serde_json::to_vec(&serde_json::json!({"next":6,"items":[item]})).unwrap()).unwrap();
        {let q=Queue::open(&dir).unwrap();assert_eq!(q.items()[0].sequence,5);assert_eq!(q.items()[0].source_sequence,Some(9));assert_eq!(q.journal.next,6);}
        assert!(!root.join("queue.json").exists());
        std::fs::write(root.join("6.item"),b"interrupted unacknowledged put").unwrap();
        {let mut q=Queue::open(&dir).unwrap();assert!(!root.join("6.item").exists());q.retire(&[5]).unwrap();}
        // Crash after index retirement but before removing its payload.
        std::fs::write(root.join("5.item"),b"retired").unwrap();
        assert!(Queue::open(&dir).unwrap().items().is_empty());assert!(!root.join("5.item").exists());
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test] fn stream_message_capacity_failure_keeps_index_and_sequence_unchanged() {
        let dir=std::env::temp_dir().join(format!("sf139-message-{}",rand::random::<u128>()));
        let mut q=Queue::open(&dir).unwrap();
        let item=Item {sequence:0,descriptor:metadata(&[1]),data:STANDARD.encode([1]),source_sequence:Some(8)};
        assert!(q.submit_message(vec![item.clone();MAX_ITEMS+1]).is_err());
        assert!(q.journal.entries.is_empty());assert_eq!(q.journal.next,0);
        q.submit_message(vec![item]).unwrap();drop(q);
        let q=Queue::open(&dir).unwrap();assert_eq!(q.items()[0].source_sequence,Some(8));assert_eq!(q.journal.next,1);drop(q);
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test] fn intake_metadata_is_optional_and_defaults_durably() {
        let dir=std::env::temp_dir().join(format!("sf139-default-{}",rand::random::<u128>()));
        let mut descriptor=serde_json::to_value(metadata(&[0])).unwrap();descriptor.as_object_mut().unwrap().remove("metadata");
        let receipt=handle(&dir,"sensor",&serde_json::json!({"descriptor":descriptor,"data_base64":"AA=="})).unwrap();
        assert_eq!(receipt["storage_acknowledged"],false);
        {let queue=Queue::open(&dir).unwrap();assert!(queue.items()[0].descriptor.metadata.is_empty());}
        std::fs::remove_dir_all(dir).unwrap();
    }
    #[test] fn authorized_topic_cannot_be_substituted() {
        assert!(handle(Path::new("/unused"),"other",&serde_json::json!({"descriptor":metadata(&[0]),"data_base64":"AA=="})).is_err());
    }
    #[test] fn bounds_and_tamper_fail_closed() {
        let mut m=metadata(&[1]);m.chunk=1;assert!(m.validate().is_err());m.chunk=0;m.size=CHUNK as u64+1;assert!(m.validate().is_err());
        let item=Item {sequence:0,descriptor:metadata(&[0]),data:STANDARD.encode([1]),source_sequence:None};assert!(item.bytes().is_err());
    }
}

/// Exact BCS payload accepted by anchor_items, also embedded in Pending.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Index {
    pub sequence: u64, pub stream: String, pub key: String, pub content_type: String,
    pub metadata: Vec<u8>, pub upload: String, pub chunk: u32, pub chunks: u32,
    pub total_size: u64, pub size: u64, pub sha256: Vec<u8>, pub identifier: String,
    pub ciphertext_size: u64, pub ciphertext_sha256: Vec<u8>,
}
impl Index {
    pub fn new(item: &Item, patch: &crate::fleet_data::QuiltItem) -> anyhow::Result<Self> {
        let m=&item.descriptor;
        Ok(Self {sequence:item.sequence,stream:m.stream.clone(),key:m.key.clone(),content_type:m.content_type.clone(),
            metadata:serde_json::to_vec(&m.metadata)?,upload:m.upload.clone(),chunk:m.chunk,chunks:m.chunks,total_size:m.total_size,
            size:m.size,sha256:m.sha256.clone(),identifier:patch.identifier.clone(),ciphertext_size:patch.size,ciphertext_sha256:hex::decode(&patch.sha256)?})
    }
    pub fn validate(&self) -> anyhow::Result<()> {
        let metadata: std::collections::BTreeMap<String,String> = serde_json::from_slice(&self.metadata)?;
        anyhow::ensure!(serde_json::to_vec(&metadata)? == self.metadata, "noncanonical opaque metadata");
        Metadata {stream:self.stream.clone(),key:self.key.clone(),content_type:self.content_type.clone(),metadata,upload:self.upload.clone(),chunk:self.chunk,chunks:self.chunks,total_size:self.total_size,size:self.size,sha256:self.sha256.clone()}.validate()?;
        crate::fleet_data::QuiltItem {identifier:self.identifier.clone(),size:self.ciphertext_size,sha256:hex::encode(&self.ciphertext_sha256)}.validate()?;
        anyhow::ensure!(self.ciphertext_size <= 131072 && self.sequence < 9_007_199_254_740_991, "opaque index bounds");
        Ok(())
    }
}

pub fn collect_streams(task: &crate::fleet_data::Task, names: &[String]) -> anyhow::Result<()> {
    if names.is_empty() { return Ok(()); }
    let store=task.services.lock().unwrap().health_sources().streams.ok_or_else(||anyhow::anyhow!("Walrus streams unavailable"))?;
    let mut store=store.lock().unwrap();
    let mut queue=Queue::open(&task.state_dir)?;
    for name in names {
        store.validate_walrus(name)?;
        if let Some((lost,messages))=store.export_batch(name,16,16*CHUNK,crate::telemetry::now_ms())? {
            anyhow::ensure!(lost == 0,"Walrus stream lost unacknowledged records");
            for message in messages {
                // A queued last chunk proves complete local custody for this
                // message. Do not recopy earlier chunks retired by prior batches
                // while its final chunk still awaits verified publication.
                if queue.items().iter().any(|i|i.descriptor.stream == *name && i.source_sequence == Some(message.sequence)) { continue; }
                // Stream messages larger than IPC chunks are broken into independently indexed patches.
                let upload=hex::encode(Sha256::digest(format!("{}/{}/{}/{name}/{}",task.sui.manifest().chain_identifier,task.config.fleet_id.parse::<sui_sdk_types::Address>()?,task.device_id.parse::<sui_sdk_types::Address>()?,message.sequence)));
                let chunks=message.data.len().max(1).div_ceil(CHUNK) as u32;
                let mut items=Vec::new();
                for chunk in 0..chunks {
                    let start=chunk as usize*CHUNK;let end=(start+CHUNK).min(message.data.len());let bytes=&message.data[start..end];
                    items.push(Item {sequence:queue.journal.next+chunk as u64,
                        descriptor:Metadata {stream:name.clone(),key:message.sequence.to_string(),content_type:"application/octet-stream".into(),metadata:Default::default(),upload:upload.clone(),chunk,chunks,total_size:message.data.len() as u64,size:bytes.len() as u64,sha256:Sha256::digest(bytes).to_vec()},data:STANDARD.encode(bytes),
                        // Only the final chunk advances the durable stream checkpoint.
                        source_sequence:if chunk+1 == chunks {Some(message.sequence)} else {None}});
                }
                if let Err(error)=queue.submit_message(items) {
                    if error.downcast_ref::<crate::streams::StreamError>().is_some_and(|e|e.code == "full") {break;}
                    return Err(error);
                }
            }
        }
    }
    Ok(())
}
