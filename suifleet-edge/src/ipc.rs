//! Native local protocol v1. Identity comes only from SO_PEERCRED, never JSON.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::{BTreeMap, VecDeque}, os::unix::fs::{MetadataExt, PermissionsExt}, path::PathBuf, sync::Arc, time::Duration};
use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader}, net::{UnixListener, UnixStream}, sync::{mpsc, oneshot, Semaphore}};

pub const MAX_FRAME: usize = 65536;
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub socket: PathBuf,
    /// Operator principals, never the daemon/component shared UID.
    #[serde(default)]
    pub administrators: Vec<u32>,
    #[serde(default)]
    pub components: BTreeMap<String, Acl>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Acl {
    pub uid: u32,
    #[serde(default)]
    pub operations: Vec<String>,
    #[serde(default)]
    pub publish: Vec<String>,
    #[serde(default)]
    pub subscribe: Vec<String>,
    /// Durable pub/sub (SF-032): AWS-style policies with MQTT topic filters.
    #[serde(default)]
    pub pubsub_policies: Vec<crate::pubsub::Policy>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub version: u32,
    pub operation: String,
    #[serde(default)]
    pub component: String,
    #[serde(default)]
    pub topic: String,
    #[serde(default)]
    pub payload: Value,
}
pub struct Pending {
    pub uid: u32,
    pub request: Request,
    pub reply: oneshot::Sender<Value>,
}
pub struct Server {
    pub requests: mpsc::Receiver<Pending>,
    task: tokio::task::JoinHandle<()>,
    socket: PathBuf,
    inode: u64,
}
impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
        if std::fs::symlink_metadata(&self.socket).is_ok_and(|m| m.ino() == self.inode) {
            let _ = std::fs::remove_file(&self.socket);
        }
    }
}
const OPS: &[&str] = &["list", "status", "start", "stop", "restart", "logs", "config", "health", "publish", "receive", "install",
    "subscribe", "unsubscribe", "ack", "messaging"];
impl Settings {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.socket.is_absolute(), "IPC socket must be absolute");
        // A shared daemon UID cannot identify a particular component and can
        // inspect sibling processes. Do not offer a cosmetic bearer-token ACL.
        let daemon_uid = unsafe { libc::geteuid() };
        anyhow::ensure!(self.components.len() <= 64 && self.administrators.len() <= 16,
            "too many IPC principals");
        anyhow::ensure!(self.components.values().map(|a| a.subscribe.len()).sum::<usize>() <= 128,
            "too many IPC subscriptions");
        let mut uids = std::collections::BTreeSet::new();
        for (name, acl) in &self.components {
            anyhow::ensure!(crate::deps::valid_name(name), "invalid IPC component name");
            anyhow::ensure!(acl.uid != 0 && acl.uid != daemon_uid && !self.administrators.contains(&acl.uid)
                && uids.insert(acl.uid), "IPC components require distinct non-root, non-daemon, non-admin UIDs");
            anyhow::ensure!(acl.operations.len() <= OPS.len() && acl.publish.len() <= 128
                && acl.subscribe.len() <= 128, "IPC ACL too large");
            anyhow::ensure!(acl.operations.iter().all(|op| OPS.contains(&op.as_str()) && op != "install" && op != "list" && op != "messaging"), "invalid component IPC operation");
            anyhow::ensure!(acl.publish.iter().chain(&acl.subscribe).all(|t| topic(t)), "invalid IPC topic");
            crate::pubsub::validate_policies(&acl.pubsub_policies)?;
        }
        anyhow::ensure!(!self.administrators.contains(&daemon_uid) || daemon_uid == 0,
            "shared daemon UID cannot be an IPC administrator");
        Ok(())
    }
    pub fn authorize(&self, uid: u32, r: &Request) -> anyhow::Result<String> {
        anyhow::ensure!(r.version == 1, "unsupported_version");
        anyhow::ensure!(OPS.contains(&r.operation.as_str()), "unknown_operation");
        if self.administrators.contains(&uid) { return Ok(format!("@operator/{uid}")); }
        let (name, acl) = self.components.iter().find(|(_, a)| a.uid == uid)
            .ok_or_else(|| anyhow::anyhow!("unauthorized"))?;
        anyhow::ensure!(r.component == *name && acl.operations.contains(&r.operation), "forbidden");
        use crate::pubsub::authorized;
        let policies = &acl.pubsub_policies;
        match r.operation.as_str() {
            "publish" => anyhow::ensure!(acl.publish.contains(&r.topic) || authorized(policies, "PublishToTopic", &r.topic), "forbidden_topic"),
            "receive" => anyhow::ensure!(acl.subscribe.contains(&r.topic) || authorized(policies, "SubscribeToTopic", &r.topic), "forbidden_topic"),
            // Revoking the policy also ends access to an existing subscription.
            "subscribe" | "unsubscribe" | "ack" => anyhow::ensure!(authorized(policies, "SubscribeToTopic", &r.topic), "forbidden_topic"),
            _ => {}
        }
        Ok(name.clone())
    }
}
fn topic(t: &str) -> bool {
    !t.is_empty() && t.len() <= 128 && t.bytes().all(|b| b.is_ascii_alphanumeric() || b"/._-".contains(&b))
}
pub fn response(result: anyhow::Result<Value>) -> Value {
    match result {
        Ok(data) => json!({"version":1,"ok":true,"data":data}),
        // Runtime diagnostics may contain private paths/commands/configuration.
        // Detailed local failure evidence remains in the daemon's private logs.
        Err(_) => json!({"version":1,"ok":false,"error":"request_failed"}),
    }
}
pub fn bind(settings: &Settings) -> anyhow::Result<Server> {
    settings.validate()?;
    let parent = settings.socket.parent().ok_or_else(|| anyhow::anyhow!("socket parent missing"))?;
    let metadata = std::fs::symlink_metadata(parent)?;
    anyhow::ensure!(metadata.is_dir() && metadata.uid() == unsafe { libc::geteuid() }
        && metadata.permissions().mode() & 0o022 == 0, "IPC socket parent must be daemon-owned and not group/world writable");
    // Never unlink a preexisting path. A stale socket requires operator clearance.
    let listener = UnixListener::bind(&settings.socket)?;
    std::fs::set_permissions(&settings.socket, std::fs::Permissions::from_mode(0o666))?;
    let inode = std::fs::symlink_metadata(&settings.socket)?.ino();
    let (sender, requests) = mpsc::channel(32);
    let slots = Arc::new(Semaphore::new(32));
    // Bounded at configuration time; no attacker-controlled UID map growth.
    let principals: BTreeMap<_, _> = settings.administrators.iter().copied()
        .chain(settings.components.values().map(|acl| acl.uid))
        .map(|uid| (uid, Arc::new(Semaphore::new(4)))).collect();
    let policy = settings.clone();
    let task = tokio::spawn(async move {
        let mut clients = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                result = listener.accept() => {
                    let Ok((stream, _)) = result else { break };
                    let Ok(cred) = stream.peer_cred() else { continue };
                    let uid = cred.uid();
                    let Some(limit) = principals.get(&uid) else { continue };
                    let Ok(principal_slot) = limit.clone().try_acquire_owned() else { continue };
                    let Ok(slot) = slots.clone().try_acquire_owned() else { continue };
                    let sender = sender.clone();
                    let policy = policy.clone();
                    clients.spawn(async move {
                        let _slot = (slot, principal_slot);
                        let _ = tokio::time::timeout(Duration::from_secs(120), connection(stream, sender, policy, uid)).await;
                    });
                }
                _ = clients.join_next(), if !clients.is_empty() => {}
            }
        }
    });
    Ok(Server { requests, task, socket: settings.socket.clone(), inode })
}
async fn connection(stream: UnixStream, sender: mpsc::Sender<Pending>, policy: Settings, uid: u32) -> anyhow::Result<()> {
    let (read, mut write) = stream.into_split();
    let mut read = BufReader::new(read);
    let mut bytes = Vec::new();
    // take() caps allocation even without a newline (read_until alone does not).
    use tokio::io::AsyncReadExt;
    let count = tokio::time::timeout(Duration::from_secs(3),
        (&mut read).take((MAX_FRAME + 1) as u64).read_until(b'\n', &mut bytes)).await??;
    let result = async {
        anyhow::ensure!(count > 0 && count <= MAX_FRAME && bytes.last() == Some(&b'\n'), "invalid_frame");
        let request: Request = serde_json::from_slice(&bytes)?;
        policy.authorize(uid, &request)?;
        let (reply, receiver) = oneshot::channel();
        sender.try_send(Pending { uid, request, reply }).map_err(|_| anyhow::anyhow!("busy"))?;
        receiver.await.map_err(Into::into)
    }.await;
    let value = match result { Ok(value) => value, Err(_) => response(Err(anyhow::anyhow!("rejected"))) };
    let mut bytes = serde_json::to_vec(&value)?;
    if bytes.len() >= MAX_FRAME { bytes = serde_json::to_vec(&response(Err(anyhow::anyhow!("response_too_large"))))?; }
    bytes.push(b'\n');
    write.write_all(&bytes).await?;
    Ok(())
}

/// Bounded, volatile inboxes. No durability/retention/bridge promise (SF-032).
#[derive(Default)]
pub struct Messages {
    queues: BTreeMap<(String, String), VecDeque<Value>>,
    health: BTreeMap<String, Value>,
}
impl Messages {
    pub fn handle(&mut self, policy: &Settings, r: &Request, principal: &str,
                  durable: Option<&std::sync::Mutex<crate::pubsub::Broker>>) -> anyhow::Result<Value> {
        match r.operation.as_str() {
            "health" => {
                anyhow::ensure!(policy.components.contains_key(&r.component), "unknown principal");
                anyhow::ensure!(["healthy", "degraded", "broken"].iter().any(|v| r.payload.as_str() == Some(v)), "invalid health");
                self.health.insert(r.component.clone(), json!({"state":r.payload,"reported_ms":crate::telemetry::now_ms()}));
                Ok(json!({"reported":true}))
            }
            "publish" => {
                let legacy = topic(&r.topic);
                anyhow::ensure!((legacy || crate::pubsub::valid_topic(&r.topic)) && serde_json::to_vec(&r.payload)?.len() <= 4096, "invalid message");
                let targets: Vec<_> = if legacy {
                    policy.components.iter().filter(|(_, a)| a.subscribe.contains(&r.topic)
                        && a.operations.iter().any(|op| op == "receive")).map(|(n, _)| n.clone()).collect()
                } else { Vec::new() };
                // Backpressure is atomic: never partially deliver a publication.
                anyhow::ensure!(targets.iter().all(|n| self.queues.get(&(n.clone(), r.topic.clone())).map_or(0, VecDeque::len) < 32), "inbox full");
                // Durable subscribers next (all or nothing), then the volatile inboxes.
                let stored = match durable {
                    Some(broker) => broker.lock().unwrap().publish(principal, &r.topic, &r.payload, None)?,
                    None => json!({"durable":0,"volatile":0}),
                };
                for name in &targets {
                    self.queues.entry((name.clone(), r.topic.clone())).or_default()
                        .push_back(json!({"sender":principal,"payload":r.payload}));
                }
                Ok(json!({"delivered":targets.len(),"durable":stored["durable"],"volatile":stored["volatile"]}))
            }
            "receive" => {
                if let Some(broker) = durable {
                    let mut broker = broker.lock().unwrap();
                    if broker.has(principal, &r.topic) {
                        return Ok(broker.receive(principal, &r.topic)?.map(|m| serde_json::to_value(m)).transpose()?.unwrap_or(Value::Null));
                    }
                }
                Ok(self.queues.get_mut(&(r.component.clone(), r.topic.clone()))
                    .and_then(VecDeque::pop_front).unwrap_or(Value::Null))
            }
            "subscribe" | "unsubscribe" | "ack" | "messaging" => {
                let broker = durable.ok_or_else(|| anyhow::anyhow!("durable pub/sub is not enabled ([pubsub])"))?;
                let mut broker = broker.lock().unwrap();
                match r.operation.as_str() {
                    "subscribe" => {
                        let qos = r.payload.get("qos").and_then(Value::as_u64).unwrap_or(1);
                        anyhow::ensure!(qos <= 1 && (r.payload.is_null() || r.payload.as_object().is_some_and(|o| o.keys().all(|k| k == "qos"))), "payload is {{\"qos\": 0|1}}");
                        broker.subscribe(principal, &r.topic, qos as u8)?;
                        Ok(json!({"subscribed":r.topic,"qos":qos}))
                    }
                    "unsubscribe" => Ok(json!({"unsubscribed":broker.unsubscribe(principal, &r.topic)?})),
                    "ack" => {
                        let id = r.payload.as_u64().ok_or_else(|| anyhow::anyhow!("payload is the message id"))?;
                        Ok(json!({"acknowledged":broker.ack(principal, &r.topic, id)?}))
                    }
                    _ => Ok(json!({"subscriptions":broker.stats(None)})),
                }
            }
            _ => anyhow::bail!("unknown operation"),
        }
    }
    pub fn health(&self, name: &str) -> Value { self.health.get(name).cloned().unwrap_or(Value::Null) }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn policy() -> Settings {
        Settings { socket: "/run/suifleet/ipc.sock".into(), administrators: vec![0],
            components: BTreeMap::from([
                ("a".into(), Acl { uid: 61001, operations: vec!["config".into(), "health".into(), "publish".into(), "receive".into()], publish: vec!["a/events".into()], subscribe: vec!["a/events".into()], pubsub_policies: vec![] }),
                ("b".into(), Acl { uid: 61002, operations: vec!["receive".into()], publish: vec![], subscribe: vec!["a/events".into()], pubsub_policies: vec![] }),
            ]) }
    }
    fn request(op: &str) -> Request {
        Request { version: 1, operation: op.into(), component: "a".into(), topic: "a/events".into(), payload: json!("healthy") }
    }
    #[test]
    fn authorization_is_exact_and_defaults_to_deny() {
        let p = policy();
        assert_eq!(p.authorize(0, &request("publish")).unwrap(), "@operator/0");
        assert!(p.authorize(61000, &request("config")).is_err());
        assert_eq!(p.authorize(61001, &request("config")).unwrap(), "a");
        assert!(p.authorize(61002, &request("config")).is_err());
        assert!(p.authorize(61001, &request("install")).is_err());
        assert!(p.authorize(61001, &request("list")).is_err());
        assert!(p.authorize(61001, &Request { component: "b".into(), ..request("config") }).is_err());
        assert!(p.authorize(61001, &Request { topic: "a/*".into(), ..request("publish") }).is_err());
        assert!(p.authorize(0, &Request { version: 2, ..request("config") }).is_err());
        assert!(p.authorize(0, &request("execute-shell")).is_err());
        assert!(serde_json::from_value::<Request>(json!({"version":1,"operation":"config","uid":0})).is_err());
    }
    #[test]
    fn shared_identity_policy_is_rejected() {
        let mut p = policy();
        p.components.get_mut("a").unwrap().uid = unsafe { libc::geteuid() };
        assert!(p.validate().is_err());
        let mut p = policy();
        p.components.get_mut("a").unwrap().uid = 61002;
        assert!(p.validate().is_err());
        let mut p = policy();
        p.components.get_mut("a").unwrap().operations.push("install".into());
        assert!(p.validate().is_err());
    }
    #[test]
    fn messaging_backpressure_is_atomic_and_health_is_bounded() {
        let p = policy();
        let mut messages = Messages::default();
        for _ in 0..32 { messages.handle(&p, &request("publish"), "a", None).unwrap(); }
        assert!(messages.handle(&p, &request("publish"), "a", None).is_err());
        let receive = request("receive");
        assert_eq!(messages.handle(&p, &receive, "a", None).unwrap()["sender"], "a");
        // b is still full: no partial publication into a's free slot.
        assert!(messages.handle(&p, &request("publish"), "a", None).is_err());
        assert_eq!(messages.queues[&("a".into(), "a/events".into())].len(), 31);
        assert!(messages.handle(&p, &Request { payload: json!("x".repeat(4097)), ..request("publish") }, "a", None).is_err());
        messages.handle(&p, &request("health"), "a", None).unwrap();
        assert_eq!(messages.health("a")["state"], "healthy");
        assert!(messages.handle(&p, &Request { payload: json!({"secret":"bad"}), ..request("health") }, "a", None).is_err());
    }
    #[tokio::test]
    async fn real_socket_uses_kernel_uid_and_rejects_bad_frames() {
        use tokio::io::AsyncReadExt;
        let dir = std::env::temp_dir().join(format!("sf027-ipc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).unwrap();
        let mut p = policy();
        p.socket = dir.join("ipc.sock");
        // Unmapped peers are closed before reading any bytes.
        p.administrators.clear();
        let mut server = bind(&p).unwrap();
        let mut denied = UnixStream::connect(&p.socket).await.unwrap();
        let mut byte = [0];
        assert_eq!(tokio::time::timeout(Duration::from_secs(1), denied.read(&mut byte)).await.unwrap().unwrap(), 0);
        assert!(server.requests.try_recv().is_err());
        p.administrators = vec![0];
        for bytes in [b"{\"version\":1,\"operation\":\"list\",\"uid\":0}\n".to_vec(),
            vec![b'x'; MAX_FRAME + 1]] {
            let (mut stream, peer) = UnixStream::pair().unwrap();
            let (sender, mut requests) = mpsc::channel(1);
            tokio::spawn(connection(peer, sender, p.clone(), 0));
            stream.write_all(&bytes).await.unwrap();
            let mut reply = Vec::new();
            stream.read_to_end(&mut reply).await.unwrap();
            assert_eq!(serde_json::from_slice::<Value>(&reply).unwrap()["ok"], false);
            assert!(requests.try_recv().is_err());
        }
        // Existing paths are preserved, including a live socket.
        assert!(bind(&p).is_err());
        assert!(p.socket.exists());
        drop(server);
        assert!(!p.socket.exists());
        std::fs::remove_dir_all(dir).unwrap();
    }

    fn durable_policy() -> Settings {
        use crate::pubsub::Policy;
        let ops = |list: &[&str]| list.iter().map(|o| o.to_string()).collect::<Vec<_>>();
        Settings {
            socket: "/tmp/x.sock".into(), administrators: vec![0],
            components: [
                ("pub".into(), Acl { uid: 62001, operations: ops(&["publish"]), publish: vec![], subscribe: vec![],
                    pubsub_policies: vec![Policy { operations: vec!["aws.greengrass#PublishToTopic".into()], resources: vec!["sensors/*".into()] }] }),
                ("sub".into(), Acl { uid: 62002, operations: ops(&["subscribe", "receive", "ack", "unsubscribe"]), publish: vec![], subscribe: vec![],
                    pubsub_policies: vec![Policy { operations: vec!["aws.greengrass#SubscribeToTopic".into()], resources: vec!["sensors/#".into()] }] }),
            ].into_iter().collect(),
        }
    }

    #[test]
    fn durable_pubsub_through_ipc_policies() {
        let p = durable_policy();
        p.validate().unwrap();
        let dir = std::env::temp_dir().join(format!("suifleet-ipc-durable-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let broker = std::sync::Mutex::new(crate::pubsub::Broker::open(&dir, crate::pubsub::Limits::default()).unwrap());
        let mut messages = Messages::default();
        let req = |op: &str, component: &str, topic: &str, payload: Value| Request { version: 1, operation: op.into(), component: component.into(), topic: topic.into(), payload };
        // Subscribe: the literal filter must be granted (wildcards are not expanded).
        let subscribe = req("subscribe", "sub", "sensors/#", json!({ "qos": 1 }));
        assert_eq!(p.authorize(62002, &subscribe).unwrap(), "sub");
        assert!(p.authorize(62002, &req("subscribe", "sub", "sensors/+", json!({}))).is_err());
        messages.handle(&p, &subscribe, "sub", Some(&broker)).unwrap();
        // Publish: the topic must match a resource glob.
        let publish = req("publish", "pub", "sensors/a/temp", json!({ "v": 1 }));
        assert_eq!(p.authorize(62001, &publish).unwrap(), "pub");
        assert!(p.authorize(62001, &req("publish", "pub", "actuators/x", json!(1))).is_err());
        let out = messages.handle(&p, &publish, "pub", Some(&broker)).unwrap();
        assert_eq!((out["durable"].as_u64(), out["delivered"].as_u64()), (Some(1), Some(0)));
        // Receive and ack.
        let receive = req("receive", "sub", "sensors/#", Value::Null);
        p.authorize(62002, &receive).unwrap();
        let message = messages.handle(&p, &receive, "sub", Some(&broker)).unwrap();
        assert_eq!((message["sender"].as_str(), message["topic"].as_str()), (Some("pub"), Some("sensors/a/temp")));
        let ack = req("ack", "sub", "sensors/#", message["id"].clone());
        assert_eq!(messages.handle(&p, &ack, "sub", Some(&broker)).unwrap()["acknowledged"], true);
        assert!(messages.handle(&p, &receive, "sub", Some(&broker)).unwrap().is_null());
        // A component cannot act on another's subscription or see fleet status.
        assert!(p.authorize(62001, &req("receive", "sub", "sensors/#", Value::Null)).is_err());
        assert!(p.authorize(62002, &req("messaging", "sub", "", Value::Null)).is_err());
        let status = messages.handle(&p, &req("messaging", "", "", Value::Null), "@operator/0", Some(&broker)).unwrap();
        assert_eq!(status["subscriptions"][0]["filter"], "sensors/#");
        // Without [pubsub] the durable operations fail closed.
        assert!(messages.handle(&p, &subscribe, "sub", None).is_err());
        // Revoking the policy ends access to the existing subscription.
        let mut revoked = durable_policy();
        revoked.components.get_mut("sub").unwrap().pubsub_policies.clear();
        assert!(revoked.authorize(62002, &receive).is_err());
    }
}
