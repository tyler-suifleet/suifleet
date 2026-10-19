//! The client-device gateway (SF-035): mutual TLS for LAN client devices, an
//! authorizing MQTT 3.1.1 proxy in front of the local broker, authenticated
//! discovery and certificate rotation over HTTPS, and unauthenticated UDP
//! discovery hints (docs/CLIENT_DEVICES.md). Identity and policy decisions
//! are `clientdevices::Authority`'s; this module only enforces them.
use crate::clientdevices::{fingerprint, Authority};
use crate::{mqtt, telemetry::now_ms};
use rcgen::SanType;
use rustls::{server::{ClientHello, ResolvesServerCert, WebPkiClientVerifier}, sign::CertifiedKey, ServerConfig};
use serde_json::{json, Value};
use std::{collections::HashMap, net::{IpAddr, SocketAddr}, sync::{Arc, Mutex, RwLock}, time::{Duration, Instant}};
use tokio::{io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt}, net::{TcpListener, TcpStream, UdpSocket}, sync::{mpsc, Semaphore}};
use tokio_rustls::{server::TlsStream, TlsAcceptor};
use tracing::{debug, info, warn};

const HANDSHAKE: Duration = Duration::from_secs(10);
/// How often a live session re-checks its identity (revocation, expiry, rotation).
const RECHECK: Duration = Duration::from_secs(1);
/// How often addresses are detected and the server certificate checked.
const REFRESH: Duration = Duration::from_secs(30);
pub const DISCOVER_MAGIC: &[u8] = b"SUIFLEET-DISCOVER/1";
/// A UDP request must be at least this long and at least as long as the reply,
/// so the responder never amplifies spoofed traffic.
pub const MIN_DISCOVER_REQUEST: usize = 512;
const MAX_DISCOVER_REPLIES_PER_SECOND: u32 = 20;
const MAX_HTTP_HEAD: usize = 8 * 1024;
const MAX_HTTP_BODY: usize = 16 * 1024;

/// Serves the current server certificate; swapped when addresses change or
/// two thirds of its lifetime have passed.
#[derive(Debug)]
struct Resolver(RwLock<Arc<CertifiedKey>>);
impl ResolvesServerCert for Resolver {
    fn resolve(&self, _: ClientHello<'_>) -> Option<Arc<CertifiedKey>> {
        Some(self.0.read().unwrap().clone())
    }
}

struct Shared {
    authority: Arc<Authority>,
    acceptor: TlsAcceptor,
    password: Option<Vec<u8>>,
    mqtt_listen: SocketAddr,
    https_listen: SocketAddr,
    /// Detected interface addresses (IPDetector equivalent).
    addresses: RwLock<Vec<IpAddr>>,
}

fn host(ip: &IpAddr) -> String {
    match ip {
        IpAddr::V4(v4) => v4.to_string(),
        IpAddr::V6(v6) => format!("[{v6}]"),
    }
}

/// Whether a listener on `listen` is reachable at `ip`.
fn serves(listen: &SocketAddr, ip: &IpAddr) -> bool {
    match listen.ip() {
        IpAddr::V4(any) if any.is_unspecified() => ip.is_ipv4(),
        IpAddr::V6(any) if any.is_unspecified() => true,
        bound => bound == *ip,
    }
}

impl Shared {
    fn endpoints(&self, listen: &SocketAddr) -> Vec<String> {
        let mut hosts = self.authority.settings.hostnames.clone();
        if listen.ip().is_unspecified() {
            hosts.extend(self.addresses.read().unwrap().iter().filter(|ip| serves(listen, ip)).map(host));
        } else {
            hosts.push(host(&listen.ip()));
        }
        hosts.into_iter().map(|h| format!("{h}:{}", listen.port())).collect()
    }
}

/// The server certificate's names: configured hostnames, addresses either
/// listener serves and specifically bound listener addresses.
fn names(settings: &crate::clientdevices::Settings, listeners: &[SocketAddr], addresses: &[IpAddr]) -> anyhow::Result<Vec<SanType>> {
    let mut names = settings.hostnames.iter().map(|h| Ok(SanType::DnsName(h.as_str().try_into()?))).collect::<anyhow::Result<Vec<_>>>()?;
    let mut ips: Vec<IpAddr> = addresses.iter().filter(|ip| listeners.iter().any(|l| serves(l, ip))).copied().collect();
    ips.extend(listeners.iter().map(|l| l.ip()).filter(|ip| !ip.is_unspecified()));
    ips.sort();
    ips.dedup();
    names.extend(ips.into_iter().map(SanType::IpAddress));
    Ok(names)
}

/// Up, non-loopback (unless included) unicast interface addresses.
pub fn local_addresses(loopback: bool, link_local: bool) -> Vec<IpAddr> {
    let mut found = Vec::new();
    let mut list: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: getifaddrs allocates a list that is only read here and freed below.
    if unsafe { libc::getifaddrs(&mut list) } != 0 {
        return found;
    }
    let mut cursor = list;
    while !cursor.is_null() {
        // SAFETY: cursor is a node of the list getifaddrs returned.
        let entry = unsafe { &*cursor };
        cursor = entry.ifa_next;
        if entry.ifa_addr.is_null() || entry.ifa_flags & libc::IFF_UP as u32 == 0 {
            continue;
        }
        // SAFETY: ifa_addr is non-null and its family selects the sockaddr type.
        let ip = unsafe {
            match (*entry.ifa_addr).sa_family as i32 {
                libc::AF_INET => IpAddr::from(u32::from_be((*(entry.ifa_addr as *const libc::sockaddr_in)).sin_addr.s_addr).to_be_bytes()),
                libc::AF_INET6 => IpAddr::from((*(entry.ifa_addr as *const libc::sockaddr_in6)).sin6_addr.s6_addr),
                _ => continue,
            }
        };
        let is_link_local = match ip {
            IpAddr::V4(v4) => v4.is_link_local(),
            IpAddr::V6(v6) => v6.segments()[0] & 0xffc0 == 0xfe80,
        };
        if ip.is_unspecified() || ip.is_multicast() || (ip.is_loopback() && !loopback) || (is_link_local && !link_local) {
            continue;
        }
        found.push(ip);
    }
    // SAFETY: the list came from getifaddrs above.
    unsafe { libc::freeifaddrs(list) };
    found.sort();
    found.dedup();
    found
}

fn certified(authority: &Authority, provider: &rustls::crypto::CryptoProvider, names: &[SanType]) -> anyhow::Result<Arc<CertifiedKey>> {
    let (chain, key) = authority.server_certificate(names, now_ms())?;
    Ok(Arc::new(CertifiedKey::new(chain, provider.key_provider.load_private_key(key)?)))
}

/// Bind every listener (so a configuration error stops startup) and serve.
pub async fn start(authority: Arc<Authority>) -> anyhow::Result<tokio::task::JoinHandle<()>> {
    let settings = authority.settings.clone();
    let mqtt_listen: SocketAddr = settings.mqtt_listen.parse()?;
    let https_listen: SocketAddr = settings.https_listen.parse()?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut roots = rustls::RootCertStore::empty();
    roots.add(authority.ca_der().clone())?;
    // Only this CA's certificates complete a handshake; the registry then
    // decides whether the exact certificate is a current identity.
    let verifier = WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider.clone()).build()?;
    let addresses = local_addresses(settings.include_loopback, settings.include_link_local);
    let listeners = [mqtt_listen, https_listen];
    let mut current_names = names(&settings, &listeners, &addresses)?;
    let resolver = Arc::new(Resolver(RwLock::new(certified(&authority, &provider, &current_names)?)));
    let mut config = ServerConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()?
        .with_client_cert_verifier(verifier)
        .with_cert_resolver(resolver.clone());
    // No resumption: every connection presents its certificate again.
    config.session_storage = Arc::new(rustls::server::NoServerSessionStorage {});
    config.send_tls13_tickets = 0;
    let shared = Arc::new(Shared {
        authority: authority.clone(),
        acceptor: TlsAcceptor::from(Arc::new(config)),
        password: settings.broker_password()?,
        mqtt_listen,
        https_listen,
        addresses: RwLock::new(addresses),
    });
    let mqtt = TcpListener::bind(mqtt_listen).await.map_err(|e| anyhow::anyhow!("cannot listen on {mqtt_listen}: {e}"))?;
    let https = TcpListener::bind(https_listen).await.map_err(|e| anyhow::anyhow!("cannot listen on {https_listen}: {e}"))?;
    let udp = match &settings.udp_discovery {
        Some(address) => Some(UdpSocket::bind(address).await.map_err(|e| anyhow::anyhow!("cannot listen on UDP {address}: {e}"))?),
        None => None,
    };
    info!(mqtt = %mqtt_listen, https = %https_listen, ca = authority.ca_sha256(), "Client-device gateway listening");
    let connections = Arc::new(Semaphore::new(settings.max_connections));
    Ok(tokio::spawn(async move {
        let refresh = {
            let shared = shared.clone();
            async move {
                let mut issued = Instant::now();
                let lifetime = Duration::from_secs(settings.server_certificate_validity_seconds);
                loop {
                    tokio::time::sleep(REFRESH).await;
                    let addresses = local_addresses(settings.include_loopback, settings.include_link_local);
                    let next = match names(&settings, &listeners, &addresses) {
                        Ok(next) => next,
                        Err(error) => { warn!(%error, "Cannot name the gateway certificate"); continue; }
                    };
                    *shared.addresses.write().unwrap() = addresses;
                    if next != current_names || issued.elapsed() >= lifetime * 2 / 3 {
                        match certified(&shared.authority, &provider, &next) {
                            Ok(key) => {
                                *resolver.0.write().unwrap() = key;
                                current_names = next;
                                issued = Instant::now();
                                info!("Client-device gateway certificate renewed");
                            }
                            Err(error) => warn!(%error, "Cannot renew the gateway certificate"),
                        }
                    }
                }
            }
        };
        let udp = async {
            match udp {
                Some(socket) => udp_discovery(&shared, socket).await,
                None => std::future::pending().await,
            }
        };
        tokio::select! {
            _ = refresh => {}
            _ = accept(shared.clone(), mqtt, connections.clone(), Kind::Mqtt) => {}
            _ = accept(shared.clone(), https, connections, Kind::Https) => {}
            _ = udp => {}
        }
    }))
}

#[derive(Clone, Copy)]
enum Kind {
    Mqtt,
    Https,
}

async fn accept(shared: Arc<Shared>, listener: TcpListener, connections: Arc<Semaphore>, kind: Kind) {
    loop {
        let (tcp, peer) = match listener.accept().await {
            Ok(accepted) => accepted,
            Err(error) => {
                // For example EMFILE: keep serving once descriptors free up.
                warn!(%error, "Client-device accept failed");
                tokio::time::sleep(Duration::from_millis(200)).await;
                continue;
            }
        };
        let Ok(permit) = connections.clone().try_acquire_owned() else {
            shared.authority.count_denial("capacity");
            continue;
        };
        let shared = shared.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let result = match kind {
                Kind::Mqtt => mqtt_session(&shared, tcp).await,
                Kind::Https => https_session(&shared, tcp).await,
            };
            if let Err(error) = result {
                debug!(%peer, %error, "Client-device connection ended");
            }
        });
    }
}

/// The TLS handshake and the presented certificate's fingerprint.
async fn handshake(shared: &Shared, tcp: TcpStream) -> anyhow::Result<(TlsStream<TcpStream>, String)> {
    tcp.set_nodelay(true)?;
    let tls = match tokio::time::timeout(HANDSHAKE, shared.acceptor.accept(tcp)).await {
        Ok(Ok(tls)) => tls,
        failed => {
            // Untrusted, expired at the TLS layer, missing or malformed certificates.
            shared.authority.count_denial("handshake");
            anyhow::bail!("TLS handshake failed: {}", failed.map_or_else(|_| "timeout".into(), |r| r.err().map(|e| e.to_string()).unwrap_or_default()));
        }
    };
    let presented = tls.get_ref().1.peer_certificates().and_then(|chain| chain.first()).map(|c| fingerprint(c))
        .ok_or_else(|| anyhow::anyhow!("no client certificate"))?;
    Ok((tls, presented))
}

/// Read one MQTT frame: its first byte, body and raw bytes; None at EOF.
async fn read_frame<R: AsyncRead + Unpin>(reader: &mut R, buffer: &mut Vec<u8>) -> anyhow::Result<Option<(u8, Vec<u8>, Vec<u8>)>> {
    loop {
        if let Some((first, start, total)) = mqtt::frame(buffer)? {
            let raw: Vec<u8> = buffer.drain(..total).collect();
            return Ok(Some((first, raw[start..].to_vec(), raw)));
        }
        let mut chunk = [0u8; 16 * 1024];
        let read = reader.read(&mut chunk).await?;
        if read == 0 {
            return Ok(None);
        }
        buffer.extend_from_slice(&chunk[..read]);
    }
}

type Pending = Arc<Mutex<HashMap<u16, Vec<bool>>>>;

/// A session the gateway ends for authorization (not a network failure): the
/// broker gets a clean DISCONNECT, so it does not publish the will on the
/// denied client's behalf.
#[derive(Debug)]
struct Denied(String);
impl std::fmt::Display for Denied {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Denied {}

/// Answer CONNECT with a refusal code and close.
async fn refuse<W: AsyncWrite + Unpin>(authority: &Authority, client: &mut W, thing: &str, code: u8, reason: &'static str) -> anyhow::Result<()> {
    authority.count_denial(reason);
    let _ = client.write_all(&mqtt::connack(code)).await;
    let _ = client.shutdown().await;
    anyhow::bail!("{thing}: CONNECT refused ({reason})")
}

async fn mqtt_session(shared: &Shared, tcp: TcpStream) -> anyhow::Result<()> {
    let authority = &shared.authority;
    let (tls, presented) = handshake(shared, tcp).await?;
    let thing = match authority.authenticate(&presented, now_ms()) {
        Ok(thing) => thing,
        Err(denial) => {
            authority.count_denial(denial.as_str());
            anyhow::bail!("certificate {presented} denied: {denial}");
        }
    };
    let (mut client_read, mut client_write) = tokio::io::split(tls);
    let mut inbound = Vec::new();
    let (first, body, _) = tokio::time::timeout(HANDSHAKE, read_frame(&mut client_read, &mut inbound)).await??
        .ok_or_else(|| anyhow::anyhow!("closed before CONNECT"))?;
    anyhow::ensure!(first == 0x10, "the first packet must be CONNECT");
    let connect = mqtt::parse_connect(&body)?;
    if connect.level != 4 {
        // MQTT 3.1.1 only: unacceptable protocol version.
        return refuse(authority, &mut client_write, &thing, 1, "protocol").await;
    }
    let now = now_ms();
    let will_permitted = connect.will.as_ref().is_none_or(|(topic, ..)| crate::pubsub::valid_topic(topic)
        && authority.permits(&presented, "mqtt:publish", &format!("mqtt:topic:{topic}"), now));
    if !authority.permits(&presented, "mqtt:connect", &format!("mqtt:clientId:{}", connect.client_id), now) || !will_permitted {
        return refuse(authority, &mut client_write, &thing, 5, "connect").await;
    }
    let broker = match tokio::time::timeout(HANDSHAKE, TcpStream::connect(&authority.settings.broker)).await {
        Ok(Ok(broker)) => broker,
        _ => return refuse(authority, &mut client_write, &thing, 3, "broker_unavailable").await,
    };
    broker.set_nodelay(true)?;
    // The device authenticated with its certificate: its own credentials are
    // not passed on; the gateway's broker credentials are.
    let mut upstream = connect.clone();
    upstream.username = authority.settings.broker_username.clone();
    upstream.password = shared.password.clone();
    // A persistent broker session keeps the subscriptions granted when it was
    // opened: resume one only under the policy it was opened with.
    if !authority.resume_session(&thing)? && !connect.clean_session {
        info!(thing, "Device groups changed since its last session: starting a clean broker session");
        upstream.clean_session = true;
    }
    let (broker_read, mut broker_write) = broker.into_split();
    broker_write.write_all(&mqtt::encode_connect(&upstream)).await?;
    let _session = authority.session(&thing);
    info!(thing, "Client device connected");
    let pending: Pending = Arc::default();
    let (local, replies) = mpsc::channel(16);
    let watchdog = async {
        loop {
            tokio::time::sleep(RECHECK).await;
            if let Err(denial) = authority.authenticate(&presented, now_ms()) {
                authority.count_denial(denial.as_str());
                return Err(anyhow::Error::new(Denied(format!("identity no longer valid: {denial}"))));
            }
        }
    };
    let result = tokio::select! {
        result = client_to_broker(authority, &presented, client_read, inbound, &mut broker_write, local, pending.clone()) => result,
        result = broker_to_client(broker_read, client_write, replies, pending) => result,
        result = watchdog => result,
    };
    if result.as_ref().is_err_and(|e| e.downcast_ref::<Denied>().is_some()) {
        let _ = broker_write.write_all(&[0xe0, 0]).await;
    }
    info!(thing, outcome = ?result.as_ref().err().map(|e| e.to_string()), "Client device disconnected");
    result
}

async fn client_to_broker<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(authority: &Authority, presented: &str, mut client: R,
    mut buffer: Vec<u8>, broker: &mut W, local: mpsc::Sender<Vec<u8>>, pending: Pending) -> anyhow::Result<()> {
    loop {
        let Some((first, body, raw)) = read_frame(&mut client, &mut buffer).await? else { return Ok(()) };
        let now = now_ms();
        match first >> 4 {
            3 => {
                let topic = mqtt::publish_topic(first & 0x0f, &body)?;
                // MQTT 3.1.1 has no refusal for a PUBLISH: an unauthorized one
                // ends the session (and is never forwarded).
                if !crate::pubsub::valid_topic(&topic) || !authority.permits(presented, "mqtt:publish", &format!("mqtt:topic:{topic}"), now) {
                    authority.count_denial("publish");
                    return Err(anyhow::Error::new(Denied(format!("publish to {topic:?} denied"))));
                }
                broker.write_all(&raw).await?;
            }
            8 => {
                anyhow::ensure!(first & 0x0f == 2, "malformed SUBSCRIBE");
                let (packet_id, filters) = mqtt::parse_subscribe(&body)?;
                let allowed: Vec<bool> = filters.iter().map(|(filter, _)| crate::pubsub::valid_filter(filter)
                    && authority.permits(presented, "mqtt:subscribe", &format!("mqtt:topicfilter:{filter}"), now)).collect();
                for _ in allowed.iter().filter(|a| !**a) {
                    authority.count_denial("subscribe");
                }
                if allowed.iter().all(|a| *a) {
                    pending.lock().unwrap().remove(&packet_id);
                    broker.write_all(&raw).await?;
                } else if allowed.iter().all(|a| !*a) {
                    // SUBACK failure (0x80) for each filter, answered here.
                    local.send(mqtt::suback(packet_id, &vec![0x80; filters.len()])).await?;
                } else {
                    let permitted: Vec<_> = filters.into_iter().zip(&allowed).filter(|(_, a)| **a).map(|(f, _)| f).collect();
                    pending.lock().unwrap().insert(packet_id, allowed);
                    broker.write_all(&mqtt::subscribe(packet_id, &permitted)).await?;
                }
            }
            // PUBACK, PUBREC, PUBREL, PUBCOMP, UNSUBSCRIBE, PINGREQ.
            4..=7 | 10 | 12 => broker.write_all(&raw).await?,
            14 => {
                broker.write_all(&raw).await?;
                return Ok(());
            }
            other => anyhow::bail!("unexpected MQTT packet type {other} from a client"),
        }
    }
}

/// Merge the broker's SUBACK for a partly refused SUBSCRIBE with the refusals.
fn merge_suback(body: &[u8], pending: &Pending) -> Option<Vec<u8>> {
    let packet_id = u16::from_be_bytes([*body.first()?, *body.get(1)?]);
    let allowed = pending.lock().unwrap().remove(&packet_id)?;
    let mut granted = body[2..].iter();
    let codes: Option<Vec<u8>> = allowed.iter().map(|a| if *a { granted.next().copied() } else { Some(0x80) }).collect();
    Some(mqtt::suback(packet_id, &codes?))
}

async fn broker_to_client<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(mut broker: R, mut client: W,
    mut local: mpsc::Receiver<Vec<u8>>, pending: Pending) -> anyhow::Result<()> {
    let mut buffer = Vec::new();
    loop {
        tokio::select! {
            // Cancel safe: completed reads are already in `buffer`.
            frame = read_frame(&mut broker, &mut buffer) => match frame? {
                None => {
                    client.flush().await?;
                    anyhow::bail!("the broker closed the connection");
                }
                Some((first, body, raw)) => {
                    let bytes = if first >> 4 == 9 { merge_suback(&body, &pending).unwrap_or(raw) } else { raw };
                    client.write_all(&bytes).await?;
                }
            },
            Some(bytes) = local.recv() => client.write_all(&bytes).await?,
        }
    }
}

/// A minimal HTTP/1.1 request: method, path and body.
async fn read_http<S: AsyncRead + Unpin>(stream: &mut S) -> anyhow::Result<(String, String, Vec<u8>)> {
    let mut buffer = Vec::new();
    let head_end = loop {
        if let Some(at) = buffer.windows(4).position(|w| w == b"\r\n\r\n") {
            break at;
        }
        anyhow::ensure!(buffer.len() <= MAX_HTTP_HEAD, "request head too large");
        let mut chunk = [0u8; 4096];
        let read = stream.read(&mut chunk).await?;
        anyhow::ensure!(read > 0, "connection closed mid-request");
        buffer.extend_from_slice(&chunk[..read]);
    };
    let head = std::str::from_utf8(&buffer[..head_end])?.to_owned();
    let mut lines = head.split("\r\n");
    let mut request = lines.next().unwrap_or_default().split(' ');
    let (method, path) = (request.next().unwrap_or_default().to_owned(), request.next().unwrap_or_default().to_owned());
    let mut length = 0usize;
    for line in lines {
        let (name, value) = line.split_once(':').ok_or_else(|| anyhow::anyhow!("malformed header"))?;
        match name.trim().to_ascii_lowercase().as_str() {
            "content-length" => length = value.trim().parse()?,
            "transfer-encoding" => anyhow::bail!("chunked requests are not supported"),
            _ => {}
        }
    }
    anyhow::ensure!(length <= MAX_HTTP_BODY, "request body too large");
    let mut body = buffer[head_end + 4..].to_vec();
    anyhow::ensure!(body.len() <= length, "more bytes than Content-Length");
    let mut rest = vec![0u8; length - body.len()];
    stream.read_exact(&mut rest).await?;
    body.extend(rest);
    Ok((method, path, body))
}

async fn https_session(shared: &Shared, tcp: TcpStream) -> anyhow::Result<()> {
    let authority = &shared.authority;
    let (mut tls, presented) = handshake(shared, tcp).await?;
    let (method, path, body) = tokio::time::timeout(HANDSHAKE, read_http(&mut tls)).await??;
    let now = now_ms();
    let (status, reply): (u16, Value) = match authority.authenticate(&presented, now) {
        Err(denial) => {
            authority.count_denial(denial.as_str());
            (403, json!({"error": denial.as_str()}))
        }
        Ok(thing) => match (method.as_str(), path.as_str()) {
            ("GET", "/discover") => match authority.discovery(&presented, &shared.endpoints(&shared.mqtt_listen), &shared.endpoints(&shared.https_listen), now) {
                Ok(document) => (200, document),
                Err(error) => {
                    warn!(thing, %error, "Discovery failed");
                    (500, json!({"error": "discovery failed"}))
                }
            },
            ("POST", "/rotate") => match std::str::from_utf8(&body).map_err(anyhow::Error::from).and_then(|csr| authority.rotate(&presented, csr, now)) {
                Ok(issued) => {
                    info!(thing, sha256 = issued.record.sha256, "Client device certificate rotated");
                    (200, authority.issued_json(&issued))
                }
                Err(error) => {
                    warn!(thing, %error, "Certificate rotation refused");
                    (400, json!({"error": "rotation refused"}))
                }
            },
            _ => (404, json!({"error": "not found"})),
        },
    };
    let body = serde_json::to_vec(&reply)?;
    let reason = match status { 200 => "OK", 400 => "Bad Request", 403 => "Forbidden", 404 => "Not Found", _ => "Internal Server Error" };
    tls.write_all(format!("HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()).as_bytes()).await?;
    tls.write_all(&body).await?;
    tls.shutdown().await?;
    Ok(())
}

/// Unauthenticated LAN hints: where the gateway listens and which CA it uses.
/// Clients trust nothing here; the mutual-TLS handshake to the endpoints and
/// the signed discovery document establish authenticity.
async fn udp_discovery(shared: &Shared, socket: UdpSocket) {
    let mut buffer = vec![0u8; 2048];
    let (mut window, mut replies) = (Instant::now(), 0u32);
    loop {
        let (length, peer) = match socket.recv_from(&mut buffer).await {
            Ok(received) => received,
            Err(error) => {
                debug!(%error, "UDP discovery receive failed");
                continue;
            }
        };
        if length < MIN_DISCOVER_REQUEST || !buffer.starts_with(DISCOVER_MAGIC) {
            continue;
        }
        if window.elapsed() >= Duration::from_secs(1) {
            (window, replies) = (Instant::now(), 0);
        }
        if replies >= MAX_DISCOVER_REPLIES_PER_SECOND {
            continue;
        }
        replies += 1;
        let authority = &shared.authority;
        let Ok(reply) = serde_json::to_vec(&json!({
            "format": 1, "service": "suifleet-client-devices", "authenticated": false,
            "gateway": authority.gateway().device_object_id, "ca_sha256": authority.ca_sha256(),
            "mqtt": shared.endpoints(&shared.mqtt_listen), "https": shared.endpoints(&shared.https_listen),
        })) else { continue };
        if reply.len() <= length {
            let _ = socket.send_to(&reply, peer).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clientdevices::tests::{authority, csr, GROUPS};
    use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName};

    fn temp() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("suifleet-gw-{}-{}", std::process::id(), rand::random::<u64>()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn free_port() -> u16 {
        std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port()
    }

    struct Fixture {
        dir: std::path::PathBuf,
        authority: Arc<Authority>,
        mqtt: u16,
        https: u16,
        broker: TcpListener,
        _gateway: tokio::task::JoinHandle<()>,
    }

    async fn fixture() -> Fixture {
        let dir = temp();
        let broker = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let (mqtt, https) = (free_port(), free_port());
        let mut built = authority(&dir, GROUPS, now_ms());
        built.settings.broker = broker.local_addr().unwrap().to_string();
        built.settings.mqtt_listen = format!("127.0.0.1:{mqtt}");
        built.settings.https_listen = format!("127.0.0.1:{https}");
        built.settings.broker_username = Some("gateway".into());
        built.settings.min_rotation_interval_seconds = 0;
        let authority = Arc::new(built);
        let gateway = start(authority.clone()).await.unwrap();
        Fixture { dir, authority, mqtt, https, broker, _gateway: gateway }
    }

    /// A client certificate for `thing` enrolled through the authority.
    fn enroll(authority: &Authority, thing: &str) -> (Vec<CertificateDer<'static>>, PrivateKeyDer<'static>, String) {
        let (key, request) = csr();
        let issued = authority.enroll(thing, &request, None, now_ms()).unwrap();
        identity(&issued.certificate_pem, key, issued.record.sha256)
    }

    fn identity(pem_text: &str, key: rcgen::KeyPair, sha256: String) -> (Vec<CertificateDer<'static>>, PrivateKeyDer<'static>, String) {
        let der = pem::parse(pem_text).unwrap().contents().to_vec();
        (vec![CertificateDer::from(der)], PrivateKeyDer::Pkcs8(key.serialize_der().into()), sha256)
    }

    async fn connect_tls(fx: &Fixture, port: u16, client: Option<(Vec<CertificateDer<'static>>, PrivateKeyDer<'static>)>)
        -> std::io::Result<tokio_rustls::client::TlsStream<TcpStream>> {
        let mut roots = rustls::RootCertStore::empty();
        roots.add(fx.authority.ca_der().clone()).unwrap();
        let builder = rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions().unwrap().with_root_certificates(roots);
        let config = match client {
            Some((chain, key)) => builder.with_client_auth_cert(chain, key).unwrap(),
            None => builder.with_no_client_auth(),
        };
        let tcp = TcpStream::connect(("127.0.0.1", port)).await?;
        let mut tls = tokio_rustls::TlsConnector::from(Arc::new(config)).connect(ServerName::try_from("127.0.0.1").unwrap(), tcp).await?;
        // TLS 1.3 reports a refused client certificate only on the first read.
        tls.flush().await?;
        Ok(tls)
    }

    async fn read_packet<S: AsyncRead + Unpin>(stream: &mut S, buffer: &mut Vec<u8>) -> Option<(u8, Vec<u8>, Vec<u8>)> {
        tokio::time::timeout(Duration::from_secs(5), read_frame(stream, buffer)).await.ok()?.ok()?
    }

    async fn http(fx: &Fixture, client: (Vec<CertificateDer<'static>>, PrivateKeyDer<'static>), request: &str, body: &str) -> (u16, Value) {
        let mut tls = connect_tls(fx, fx.https, Some(client)).await.unwrap();
        tls.write_all(format!("{request} HTTP/1.1\r\nHost: gw\r\nContent-Length: {}\r\n\r\n{body}", body.len()).as_bytes()).await.unwrap();
        let mut response = Vec::new();
        tls.read_to_end(&mut response).await.unwrap();
        let text = String::from_utf8(response).unwrap();
        let (head, body) = text.split_once("\r\n\r\n").unwrap();
        (head[9..12].parse().unwrap(), serde_json::from_str(body).unwrap())
    }

    #[tokio::test]
    async fn mqtt_proxy_authenticates_authorizes_and_ends_revoked_sessions() {
        let fx = fixture().await;
        let (chain, key, _) = enroll(&fx.authority, "sensor-1");
        let pair = || (chain.clone(), key.clone_key());

        // No client certificate, or one from another CA: no session.
        let refused = |result: std::io::Result<tokio_rustls::client::TlsStream<TcpStream>>| async move {
            match result {
                Err(_) => true,
                Ok(mut tls) => {
                    let mut byte = [0u8; 1];
                    let _ = tls.write_all(&mqtt::connect("sensor-1", None, None, 30, true)).await;
                    matches!(tokio::time::timeout(Duration::from_secs(5), tls.read(&mut byte)).await, Ok(Ok(0) | Err(_)))
                }
            }
        };
        assert!(refused(connect_tls(&fx, fx.mqtt, None).await).await, "no client certificate");
        let other = rcgen::KeyPair::generate().unwrap();
        let foreign = rcgen::CertificateParams::new(vec![]).unwrap().self_signed(&other).unwrap();
        let untrusted = (vec![foreign.der().clone()], PrivateKeyDer::Pkcs8(other.serialize_der().into()));
        assert!(refused(connect_tls(&fx, fx.mqtt, Some(untrusted)).await).await, "untrusted certificate");

        // A foreign client ID is refused with CONNACK 5 before the broker is contacted.
        let mut tls = connect_tls(&fx, fx.mqtt, Some(pair())).await.unwrap();
        tls.write_all(&mqtt::connect("sensor-9", None, None, 30, true)).await.unwrap();
        let mut buffer = Vec::new();
        assert_eq!(read_packet(&mut tls, &mut buffer).await.unwrap().2, mqtt::connack(5));

        // Its own client ID: CONNECT reaches the broker with the gateway's
        // credentials instead of the device's.
        let mut tls = connect_tls(&fx, fx.mqtt, Some(pair())).await.unwrap();
        tls.write_all(&mqtt::connect("sensor-1", Some("device-user"), Some(b"device-pass"), 30, true)).await.unwrap();
        let (mut broker, _) = tokio::time::timeout(Duration::from_secs(5), fx.broker.accept()).await.unwrap().unwrap();
        let mut upstream = Vec::new();
        let (first, body, _) = read_packet(&mut broker, &mut upstream).await.unwrap();
        assert_eq!(first, 0x10);
        let forwarded = mqtt::parse_connect(&body).unwrap();
        assert_eq!((forwarded.client_id.as_str(), forwarded.username.as_deref(), forwarded.password), ("sensor-1", Some("gateway"), None));
        broker.write_all(&mqtt::connack(0)).await.unwrap();
        let mut buffer = Vec::new();
        assert_eq!(read_packet(&mut tls, &mut buffer).await.unwrap().2, mqtt::connack(0));

        // SUBSCRIBE: refused filters are answered 0x80 in place; granted ones forwarded.
        tls.write_all(&mqtt::subscribe(7, &[("commands/#".into(), 1), ("commands/+".into(), 1)])).await.unwrap();
        let (_, body, _) = read_packet(&mut broker, &mut upstream).await.unwrap();
        assert_eq!(mqtt::parse_subscribe(&body).unwrap(), (7, vec![("commands/+".into(), 1)]));
        broker.write_all(&mqtt::suback(7, &[1])).await.unwrap();
        assert_eq!(read_packet(&mut tls, &mut buffer).await.unwrap().2, mqtt::suback(7, &[0x80, 1]));
        tls.write_all(&mqtt::subscribe(8, &[("other/#".into(), 0)])).await.unwrap();
        assert_eq!(read_packet(&mut tls, &mut buffer).await.unwrap().2, mqtt::suback(8, &[0x80]), "answered locally");

        // An authorized PUBLISH is forwarded byte for byte; broker traffic flows back.
        let publish = mqtt::publish("sensors/sensor-1/temp", b"21", 1, Some(3), false);
        tls.write_all(&publish).await.unwrap();
        assert_eq!(read_packet(&mut broker, &mut upstream).await.unwrap().2, publish);
        broker.write_all(&mqtt::puback(3)).await.unwrap();
        assert_eq!(read_packet(&mut tls, &mut buffer).await.unwrap().2, mqtt::puback(3));

        // Revocation ends the live session within the re-check interval.
        fx.authority.revoke("sensor-1", now_ms()).unwrap();
        let mut byte = [0u8; 1];
        let ended = tokio::time::timeout(Duration::from_secs(5), tls.read(&mut byte)).await.unwrap();
        assert!(matches!(ended, Ok(0) | Err(_)), "{ended:?}");
        assert_eq!(read_packet(&mut broker, &mut upstream).await.unwrap().0, 0xe0, "DISCONNECT, so the broker drops the will");
        assert!(refused(connect_tls(&fx, fx.mqtt, Some(pair())).await).await, "revoked identity reconnects");
        let status = fx.authority.status(now_ms());
        assert!(status["denied"]["revoked"].as_u64().unwrap() >= 2, "{status}");
        assert!(status["denied"]["handshake"].as_u64().unwrap() >= 2, "{status}");
        std::fs::remove_dir_all(&fx.dir).unwrap();
    }

    #[tokio::test]
    async fn unauthorized_publish_ends_the_session_without_forwarding() {
        let fx = fixture().await;
        let (chain, key, _) = enroll(&fx.authority, "sensor-2");
        let mut tls = connect_tls(&fx, fx.mqtt, Some((chain, key))).await.unwrap();
        tls.write_all(&mqtt::connect("sensor-2", None, None, 30, true)).await.unwrap();
        let (mut broker, _) = fx.broker.accept().await.unwrap();
        let mut upstream = Vec::new();
        read_packet(&mut broker, &mut upstream).await.unwrap();
        broker.write_all(&mqtt::connack(0)).await.unwrap();
        let mut buffer = Vec::new();
        read_packet(&mut tls, &mut buffer).await.unwrap();
        // Another thing's topic (the policy variable binds the caller).
        tls.write_all(&mqtt::publish("sensors/sensor-1/temp", b"x", 0, None, false)).await.unwrap();
        let mut byte = [0u8; 1];
        assert!(matches!(tokio::time::timeout(Duration::from_secs(5), tls.read(&mut byte)).await.unwrap(), Ok(0) | Err(_)));
        let (first, _, _) = read_packet(&mut broker, &mut upstream).await.expect("DISCONNECT");
        assert_eq!(first, 0xe0, "a clean DISCONNECT (no will), never the publish");
        assert!(read_packet(&mut broker, &mut upstream).await.is_none(), "nothing forwarded");
        assert_eq!(fx.authority.status(now_ms())["denied"]["publish"], 1);
        std::fs::remove_dir_all(&fx.dir).unwrap();
    }

    #[tokio::test]
    async fn https_discovery_and_rotation() {
        let fx = fixture().await;
        let (chain, key, sha) = enroll(&fx.authority, "sensor-3");
        let (status, envelope) = http(&fx, (chain.clone(), key.clone_key()), "GET /discover", "").await;
        assert_eq!(status, 200, "{envelope}");
        let document = &envelope["document"];
        assert_eq!(document["mqtt"], json!([format!("127.0.0.1:{}", fx.mqtt)]));
        assert_eq!(document["certificate"]["sha256"], sha);
        assert_eq!(http(&fx, (chain.clone(), key.clone_key()), "GET /nothing", "").await.0, 404);
        assert_eq!(http(&fx, (chain.clone(), key.clone_key()), "POST /rotate", "garbage").await.0, 400);

        // Rotation over the authenticated channel; the new certificate works.
        let (new_key, request) = csr();
        let (status, issued) = http(&fx, (chain.clone(), key.clone_key()), "POST /rotate", &request).await;
        assert_eq!(status, 200, "{issued}");
        let (new_chain, new_private, new_sha) = identity(issued["certificate"].as_str().unwrap(), new_key, issued["sha256"].as_str().unwrap().into());
        let (status, envelope) = http(&fx, (new_chain.clone(), new_private.clone_key()), "GET /discover", "").await;
        assert_eq!(status, 200);
        let document = &envelope["document"];
        assert_eq!(document["certificate"]["sha256"], new_sha);
        // The superseded certificate may not rotate again, and once revoked
        // the HTTPS side reports why.
        assert_eq!(http(&fx, (chain.clone(), key.clone_key()), "POST /rotate", &csr().1).await.0, 400);
        fx.authority.revoke("sensor-3", now_ms()).unwrap();
        assert_eq!(http(&fx, (new_chain, new_private), "GET /discover", "").await, (403, json!({"error": "revoked"})));
        std::fs::remove_dir_all(&fx.dir).unwrap();
    }

    #[test]
    fn names_and_endpoints_follow_listeners() {
        let any4: SocketAddr = "0.0.0.0:8883".parse().unwrap();
        let bound: SocketAddr = "10.0.0.5:8443".parse().unwrap();
        let v6: IpAddr = "fd00::1".parse().unwrap();
        let v4: IpAddr = "10.0.0.7".parse().unwrap();
        assert!(serves(&any4, &v4) && !serves(&any4, &v6));
        assert!(serves(&"[::]:1".parse().unwrap(), &v6));
        assert!(!serves(&bound, &v4));
        let dir = temp();
        let mut settings = crate::clientdevices::tests::settings(&dir, GROUPS);
        settings.hostnames = vec!["gateway.local".into()];
        let sans = names(&settings, &[any4, bound], &[v4, v6]).unwrap();
        assert_eq!(sans, vec![SanType::DnsName("gateway.local".try_into().unwrap()), SanType::IpAddress("10.0.0.5".parse().unwrap()),
                              SanType::IpAddress(v4)]);
        assert_eq!(host(&v6), "[fd00::1]");
        assert!(local_addresses(true, false).iter().any(|ip| ip.is_loopback()));
        assert!(!local_addresses(false, false).iter().any(|ip| ip.is_loopback()));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[tokio::test]
    async fn udp_hints_never_amplify() {
        let dir = temp();
        let port = free_port();
        let mut built = authority(&dir, GROUPS, now_ms());
        built.settings.udp_discovery = Some(format!("127.0.0.1:{port}"));
        built.settings.mqtt_listen = format!("127.0.0.1:{}", free_port());
        built.settings.https_listen = format!("127.0.0.1:{}", free_port());
        let authority = Arc::new(built);
        let _gateway = start(authority.clone()).await.unwrap();
        let client = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let mut reply = vec![0u8; 2048];
        // Too short or wrong magic: silence.
        client.send_to(DISCOVER_MAGIC, ("127.0.0.1", port)).await.unwrap();
        let mut bad = b"NOT-SUIFLEET".to_vec();
        bad.resize(1200, 0);
        client.send_to(&bad, ("127.0.0.1", port)).await.unwrap();
        assert!(tokio::time::timeout(Duration::from_millis(500), client.recv_from(&mut reply)).await.is_err());
        let mut request = DISCOVER_MAGIC.to_vec();
        request.resize(1200, 0);
        client.send_to(&request, ("127.0.0.1", port)).await.unwrap();
        let (length, _) = tokio::time::timeout(Duration::from_secs(5), client.recv_from(&mut reply)).await.unwrap().unwrap();
        assert!(length <= request.len());
        let hint: Value = serde_json::from_slice(&reply[..length]).unwrap();
        assert_eq!(hint["ca_sha256"], authority.ca_sha256());
        assert_eq!(hint["authenticated"], false);
        std::fs::remove_dir_all(dir).unwrap();
    }
}
