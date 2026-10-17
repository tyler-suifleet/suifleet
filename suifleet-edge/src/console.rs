//! SF-079 local support console, protocol v1 (docs/LOCAL_CONSOLE.md).
//!
//! An HTTP/1.1 API and a static UI on a loopback address only. Every API call
//! is authenticated (a configured console token, or a session created from
//! one), authorized per user grant and audited. Sessions are cookies bound to
//! a CSRF token and the configured origins; the Host header must name a
//! configured origin (DNS rebinding). Output is an explicit field allowlist,
//! redacted and bounded. Read-only views come from the daemon's state files,
//! like telemetry; support operations run in the poll-loop owner.
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io::{Read, Seek, SeekFrom, Write},
    net::SocketAddr,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::{mpsc, oneshot, Semaphore},
};
use tracing::{info, warn};

pub const VERSION: u64 = 1;
const MAX_HEAD: usize = 8192;
const MAX_BODY: usize = 4096;
const MAX_RESPONSE: usize = 65536;
const MAX_USERS: usize = 16;
const MAX_SESSIONS: usize = 32;
const MAX_CONNECTIONS: usize = 16;
const MAX_LOG_LINES: usize = 200;
const MAX_LOG_BYTES: u64 = 8 * 1024;
const MAX_LINE: usize = 512;
const MAX_PAGE: usize = 100;
const MAX_REASONS: usize = 32;
const MAX_DEPTH: usize = 16;
const READ_TIMEOUT: Duration = Duration::from_secs(5);
const CONTROL_TIMEOUT: Duration = Duration::from_secs(60);
const COOKIE: &str = "suifleet_console";
const TOKEN_PREFIX: &str = "sfc_";
const REDACTED: &str = "[redacted]";

/// Read grants first, then the support operations they may enable.
pub const GRANTS: &[&str] = &["inventory", "health", "deployments", "logs", "configuration", "audit", "start", "stop", "restart"];
pub const SUPPORT: &[&str] = &["start", "stop", "restart"];
/// The only inventory fields that leave the daemon.
pub const INVENTORY_FIELDS: &[&str] = &["component", "version", "health", "deployment", "installed", "supervised", "restarts", "since_ms", "last_exit"];

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    /// Loopback only; remote access is SSH port forwarding (as upstream).
    #[serde(default = "default_listen")]
    pub listen: SocketAddr,
    /// Browser origins (`http://HOST:PORT`, loopback hosts). Default: the
    /// listen port on 127.0.0.1 and localhost. A forwarded port is listed here.
    #[serde(default)]
    pub origins: Vec<String>,
    /// Support operations this device enables; a user also needs the grant.
    #[serde(default)]
    pub support_operations: Vec<String>,
    pub users: BTreeMap<String, User>,
    #[serde(default = "default_idle")]
    pub session_idle_seconds: u64,
    #[serde(default = "default_session")]
    pub session_max_seconds: u64,
    #[serde(default = "default_audit")]
    pub audit_max_bytes: u64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct User {
    /// SHA-256 (lowercase hex) of a token from `suifleet --console-token`.
    /// The token itself is never stored on the device.
    pub token_sha256: String,
    pub grants: Vec<String>,
    /// Unix seconds after which the token is refused.
    #[serde(default)]
    pub expires_unix: Option<u64>,
}

fn default_listen() -> SocketAddr {
    "127.0.0.1:1441".parse().unwrap()
}
fn default_idle() -> u64 {
    900
}
fn default_session() -> u64 {
    8 * 3600
}
fn default_audit() -> u64 {
    1024 * 1024
}

impl Settings {
    pub fn validate(&self) -> anyhow::Result<()> {
        anyhow::ensure!(self.listen.ip().is_loopback() && self.listen.port() != 0,
            "console.listen must be a loopback address with a fixed port; use SSH port forwarding for remote access");
        for origin in &self.origins {
            authority(origin).ok_or_else(|| anyhow::anyhow!("console origin {origin:?} must be http://HOST:PORT with a loopback host"))?;
        }
        anyhow::ensure!(self.origins.len() <= 8, "too many console origins");
        anyhow::ensure!(self.support_operations.iter().all(|op| SUPPORT.contains(&op.as_str())),
            "console.support_operations must be among {SUPPORT:?}");
        anyhow::ensure!(!self.users.is_empty() && self.users.len() <= MAX_USERS, "console needs 1..={MAX_USERS} users");
        let mut hashes = std::collections::BTreeSet::new();
        for (name, user) in &self.users {
            anyhow::ensure!(crate::deps::valid_name(name) && name.len() <= 64, "invalid console user name");
            anyhow::ensure!(user.token_sha256.len() == 64 && user.token_sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                && hashes.insert(&user.token_sha256), "console user {name}: token_sha256 must be distinct lowercase SHA-256 hex");
            anyhow::ensure!(!user.grants.is_empty() && user.grants.len() <= GRANTS.len()
                && user.grants.iter().all(|g| GRANTS.contains(&g.as_str())), "console user {name}: grants must be among {GRANTS:?}");
            for grant in &user.grants {
                anyhow::ensure!(!SUPPORT.contains(&grant.as_str()) || self.support_operations.contains(grant),
                    "console user {name}: grant {grant} is not an enabled support operation");
            }
        }
        anyhow::ensure!((60..=3600).contains(&self.session_idle_seconds), "console.session_idle_seconds must be 60..=3600");
        anyhow::ensure!((60..=8 * 3600).contains(&self.session_max_seconds), "console.session_max_seconds must be 60..=28800");
        anyhow::ensure!((64 * 1024..=64 * 1024 * 1024).contains(&self.audit_max_bytes), "console.audit_max_bytes must be 64 KiB..=64 MiB");
        Ok(())
    }

    fn allowed_origins(&self) -> Vec<String> {
        if !self.origins.is_empty() {
            return self.origins.clone();
        }
        let port = self.listen.port();
        let mut origins = vec![format!("http://127.0.0.1:{port}"), format!("http://localhost:{port}")];
        if self.listen.is_ipv6() {
            origins.push(format!("http://[::1]:{port}"));
        }
        origins
    }
}

/// `HOST:PORT` of an `http://HOST:PORT` origin whose host is loopback.
fn authority(origin: &str) -> Option<String> {
    let rest = origin.strip_prefix("http://")?;
    let (host, port) = rest.rsplit_once(':')?;
    let port: u16 = port.parse().ok().filter(|p| *p != 0)?;
    let loopback = host.eq_ignore_ascii_case("localhost")
        || host.parse::<std::net::Ipv4Addr>().is_ok_and(|ip| ip.is_loopback())
        || host.strip_prefix('[').and_then(|h| h.strip_suffix(']')).and_then(|h| h.parse::<std::net::Ipv6Addr>().ok()).is_some_and(|ip| ip.is_loopback());
    loopback.then(|| format!("{}:{port}", host.to_ascii_lowercase()))
}

/// A new console token and the hash to configure for it.
pub fn new_token() -> (String, String) {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    let token = format!("{TOKEN_PREFIX}{}", hex::encode(bytes));
    let hash = hex::encode(Sha256::digest(token.as_bytes()));
    (token, hash)
}

fn token_hash(token: &str) -> Option<[u8; 32]> {
    // Only generated tokens: a guessable operator-chosen password is refused.
    let digits = token.strip_prefix(TOKEN_PREFIX)?;
    (digits.len() == 64 && digits.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
        .then(|| Sha256::digest(token.as_bytes()).into())
}

fn equal(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn random_hex() -> String {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::rngs::OsRng.fill_bytes(&mut bytes);
    hex::encode(bytes)
}

// ---------------------------------------------------------------- decisions

/// Contract v1 outcome before any operation runs (see the SF-064 reference
/// vectors): version, then authentication, then origin, then grant.
pub fn decide(version: &Value, authenticated: bool, origin_ok: bool, operation: &str, grants: &[String]) -> Result<(), &'static str> {
    if version.as_u64() != Some(VERSION) {
        return Err("unsupported-version");
    }
    if !authenticated {
        return Err("unauthenticated");
    }
    if !origin_ok {
        return Err("invalid-origin");
    }
    if !grants.iter().any(|g| g == operation) {
        return Err("denied");
    }
    Ok(())
}

/// Keep only allowlisted inventory fields, redacted.
pub fn allow(fields: &Map<String, Value>) -> Map<String, Value> {
    fields.iter().filter(|(k, _)| INVENTORY_FIELDS.contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), redact_value(v, 0))).collect()
}

// ---------------------------------------------------------------- redaction

const SECRET_WORDS: &[&str] = &["key", "token", "secret", "password", "passwd", "passphrase", "credential",
    "private", "auth", "cookie", "session", "signature", "mnemonic", "seed", "pin", "cert", "csrf"];
const SCHEMES: &[&str] = &["bearer", "basic", "token", "digest"];

pub fn secret_name(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    SECRET_WORDS.iter().any(|w| lower.contains(w))
}

/// Recursively replace values under secret-looking names and scrub strings.
pub fn redact_value(value: &Value, depth: usize) -> Value {
    if depth > MAX_DEPTH {
        return json!("[truncated]");
    }
    match value {
        Value::Object(map) => Value::Object(map.iter().map(|(k, v)| {
            (redact_text(k), if secret_name(k) { json!(REDACTED) } else { redact_value(v, depth + 1) })
        }).collect()),
        Value::Array(items) => Value::Array(items.iter().map(|v| redact_value(v, depth + 1)).collect()),
        Value::String(text) => Value::String(redact_text(text)),
        other => other.clone(),
    }
}

fn word_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || "_-+/.~".contains(c)
}

fn secret_value(word: &str) -> bool {
    let lower = word.to_ascii_lowercase();
    lower.starts_with("suiprivkey") || word.starts_with(TOKEN_PREFIX)
        || ((word.starts_with("AKIA") || word.starts_with("ASIA")) && word.len() == 20 && word.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit()))
        || (word.starts_with("eyJ") && word.matches('.').count() == 2)
        // Raw key material (device.key is bare hex); 0x-prefixed object IDs stay.
        || (word.len() >= 64 && word.bytes().all(|b| b.is_ascii_hexdigit()))
}

/// Scrub PEM blocks, known secret formats and `name=value`/`name: value`
/// pairs whose name looks secret. Over-redaction is preferred to leaks.
pub fn redact_text(text: &str) -> String {
    let mut text = text.to_owned();
    while let Some(start) = text.find("-----BEGIN") {
        let end = text[start..].find("-----END").map(|e| start + e + 8)
            .map(|e| text[e..].find("-----").map(|t| e + t + 5).unwrap_or(text.len()))
            .unwrap_or(text.len());
        text.replace_range(start..end, REDACTED);
    }
    let mut out = String::with_capacity(text.len());
    let mut redact_next = false;
    let mut pending_name = false;
    let mut chars = text.char_indices().peekable();
    while let Some(&(start, c)) = chars.peek() {
        let is_word = word_char(c);
        let mut end = start;
        while let Some(&(i, c)) = chars.peek() {
            if word_char(c) != is_word { break; }
            end = i + c.len_utf8();
            chars.next();
        }
        let segment = &text[start..end];
        if is_word {
            let scheme = SCHEMES.contains(&segment.to_ascii_lowercase().as_str());
            if redact_next && scheme {
                out.push_str(segment);
            } else if redact_next || secret_value(segment) {
                out.push_str(REDACTED);
                redact_next = false;
            } else {
                out.push_str(segment);
                if scheme { redact_next = true; }
            }
            pending_name = !redact_next && secret_name(segment);
        } else {
            out.push_str(segment);
            // `name=value`, `name: value`, `"name": "value"`.
            if pending_name && segment.len() <= 4 && (segment.contains('=') || segment.contains(':')) {
                redact_next = true;
            } else if redact_next && !segment.chars().all(|c| c == ' ' || c == '"' || c == '\'') {
                redact_next = false;
            }
            pending_name = false;
        }
    }
    out
}

fn bounded(text: &str, limit: usize) -> String {
    crate::telemetry::bounded(&redact_text(text), limit)
}

// ---------------------------------------------------------------- audit

/// Append-only JSON lines under `<state_dir>/console`, rotated at a bound.
pub struct Audit {
    path: PathBuf,
    max_bytes: u64,
}

impl Audit {
    pub fn open(state_dir: &Path, max_bytes: u64) -> anyhow::Result<Self> {
        use std::os::unix::fs::PermissionsExt;
        let dir = state_dir.join("console");
        std::fs::create_dir_all(&dir)?;
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))?;
        Ok(Self { path: dir.join("audit.jsonl"), max_bytes })
    }

    /// `durable`: synced before return (a support operation runs only after it).
    pub fn record(&self, entry: &Value, durable: bool) -> anyhow::Result<()> {
        let mut line = serde_json::to_vec(entry)?;
        line.push(b'\n');
        let size = std::fs::symlink_metadata(&self.path).map(|m| m.len()).unwrap_or(0);
        if size + line.len() as u64 > self.max_bytes {
            std::fs::rename(&self.path, self.path.with_extension("jsonl.1"))?;
        }
        let mut file = std::fs::OpenOptions::new().append(true).create(true).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW).open(&self.path)?;
        file.write_all(&line)?;
        if durable {
            file.sync_data()?;
        }
        Ok(())
    }

    fn tail(&self, limit: usize) -> anyhow::Result<Vec<Value>> {
        let mut file = match std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&self.path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
            other => other?,
        };
        let size = file.metadata()?.len();
        file.seek(SeekFrom::Start(size.saturating_sub(32 * 1024)))?;
        let mut bytes = Vec::new();
        file.take(32 * 1024).read_to_end(&mut bytes)?;
        let text = String::from_utf8_lossy(&bytes);
        let mut lines: Vec<&str> = text.lines().collect();
        if size > 32 * 1024 && !lines.is_empty() {
            lines.remove(0); // Cut mid-line.
        }
        let skip = lines.len().saturating_sub(limit);
        Ok(lines[skip..].iter().filter_map(|l| serde_json::from_str(l).ok()).collect())
    }
}

// ---------------------------------------------------------------- server

/// A support operation for the poll-loop owner. Dropped unanswered when the
/// console has already given up (`reply` closed).
pub struct Control {
    pub operation: String,
    pub component: String,
    pub reply: oneshot::Sender<anyhow::Result<Value>>,
}

pub struct Context {
    pub state_dir: PathBuf,
    pub sync: crate::telemetry::SharedSync,
    pub started_ms: u64,
    pub poll_seconds: u64,
}

pub struct Server {
    pub controls: mpsc::Receiver<Control>,
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}

struct Session {
    user: String,
    csrf: String,
    created: Instant,
    last: Instant,
}

struct Shared {
    settings: Settings,
    origins: Vec<String>,
    authorities: Vec<String>,
    context: Context,
    configs: crate::configuration::Store,
    audit: Audit,
    /// SHA-256 of the session cookie value → session.
    sessions: Mutex<BTreeMap<[u8; 32], Session>>,
    controls: mpsc::Sender<Control>,
}

pub async fn bind(settings: &Settings, context: Context) -> anyhow::Result<Server> {
    settings.validate()?;
    let listener = TcpListener::bind(settings.listen).await?;
    let origins = settings.allowed_origins();
    let authorities = origins.iter().filter_map(|o| authority(o)).collect();
    let configs = crate::configuration::Store::open(&context.state_dir, Path::new("/nonexistent"))?;
    let audit = Audit::open(&context.state_dir, settings.audit_max_bytes)?;
    let (sender, controls) = mpsc::channel(4);
    let shared = Arc::new(Shared {
        settings: settings.clone(), origins, authorities, context, configs, audit,
        sessions: Mutex::new(BTreeMap::new()), controls: sender,
    });
    info!(listen = %settings.listen, "Local support console listening (loopback only)");
    let slots = Arc::new(Semaphore::new(MAX_CONNECTIONS));
    let task = tokio::spawn(async move {
        let mut clients = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                result = listener.accept() => {
                    let Ok((stream, peer)) = result else { continue };
                    if !peer.ip().is_loopback() { continue; }
                    let Ok(slot) = slots.clone().try_acquire_owned() else { continue };
                    let shared = shared.clone();
                    clients.spawn(async move {
                        let _slot = slot;
                        let _ = tokio::time::timeout(CONTROL_TIMEOUT + READ_TIMEOUT * 2, connection(stream, peer, shared)).await;
                    });
                }
                _ = clients.join_next(), if !clients.is_empty() => {}
            }
        }
    });
    Ok(Server { controls, task })
}

#[derive(Debug, Default)]
struct Request {
    method: String,
    path: String,
    headers: BTreeMap<String, String>,
    body: Vec<u8>,
}

/// Headers that must not repeat (an ambiguous Host/Origin/credential).
const SINGLE: &[&str] = &["host", "origin", "cookie", "authorization", "content-length", "content-type", "x-suifleet-csrf", "sec-fetch-site"];

async fn read_request(stream: &mut TcpStream) -> Result<Request, u16> {
    let mut buffer = Vec::with_capacity(1024);
    let mut chunk = [0u8; 2048];
    let head_end = loop {
        if let Some(i) = buffer.windows(4).position(|w| w == b"\r\n\r\n") { break i; }
        if buffer.len() > MAX_HEAD { return Err(431); }
        let n = stream.read(&mut chunk).await.map_err(|_| 400u16)?;
        if n == 0 { return Err(400); }
        buffer.extend_from_slice(&chunk[..n]);
    };
    if head_end > MAX_HEAD { return Err(431); }
    let head = std::str::from_utf8(&buffer[..head_end]).map_err(|_| 400u16)?;
    let mut lines = head.split("\r\n");
    let mut start = lines.next().unwrap_or_default().split(' ');
    let (method, path, protocol) = (start.next().unwrap_or_default(), start.next().unwrap_or_default(), start.next().unwrap_or_default());
    if start.next().is_some() || !["HTTP/1.1", "HTTP/1.0"].contains(&protocol) || !path.starts_with('/')
        || method.is_empty() || !method.bytes().all(|b| b.is_ascii_uppercase()) {
        return Err(400);
    }
    let mut request = Request { method: method.into(), path: path.into(), ..Default::default() };
    for line in lines {
        let (name, value) = line.split_once(':').ok_or(400u16)?;
        // No obsolete line folding, no whitespace before the colon.
        if name.is_empty() || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b)) { return Err(400); }
        let name = name.to_ascii_lowercase();
        if name == "transfer-encoding" { return Err(501); }
        let value = value.trim_matches(|c| c == ' ' || c == '\t');
        if value.bytes().any(|b| b < 0x20 && b != b'\t') { return Err(400); }
        if request.headers.insert(name.clone(), value.into()).is_some() && SINGLE.contains(&name.as_str()) {
            return Err(400);
        }
    }
    let length: usize = match request.headers.get("content-length") {
        Some(v) => v.parse().map_err(|_| 400u16)?,
        None => 0,
    };
    if length > MAX_BODY { return Err(413); }
    let mut body = buffer[head_end + 4..].to_vec();
    while body.len() < length {
        let n = stream.read(&mut chunk).await.map_err(|_| 400u16)?;
        if n == 0 { return Err(400); }
        body.extend_from_slice(&chunk[..n]);
    }
    if body.len() != length { return Err(400); }
    request.body = body;
    Ok(request)
}

struct Response {
    status: u16,
    content_type: &'static str,
    body: Vec<u8>,
    cookie: Option<String>,
}

impl Response {
    fn json(status: u16, value: Value) -> Self {
        Self { status, content_type: "application/json", body: serde_json::to_vec(&value).unwrap_or_default(), cookie: None }
    }
    fn outcome(status: &str) -> Self {
        Self::json(http_status(status), json!({"version": VERSION, "status": status}))
    }
}

fn http_status(status: &str) -> u16 {
    match status {
        "ok" => 200,
        "unauthenticated" => 401,
        "invalid-origin" | "denied" => 403,
        "not-found" => 404,
                "busy" | "unavailable" => 503,
        "failed" | "unknown" => 500,
        _ => 400,
    }
}

const SECURITY_HEADERS: &str = "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n\
Referrer-Policy: no-referrer\r\nCross-Origin-Resource-Policy: same-origin\r\nCross-Origin-Opener-Policy: same-origin\r\n\
Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\r\n";

async fn write_response(stream: &mut TcpStream, response: Response) {
    let reason = match response.status { 200 => "OK", 401 => "Unauthorized", 403 => "Forbidden", 404 => "Not Found", 405 => "Method Not Allowed",
        413 => "Payload Too Large", 421 => "Misdirected Request", 431 => "Request Header Fields Too Large", 500 => "Internal Server Error",
        501 => "Not Implemented", 503 => "Service Unavailable", _ => "Bad Request" };
    let mut head = format!("HTTP/1.1 {} {reason}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n{SECURITY_HEADERS}",
        response.status, response.content_type, response.body.len());
    if let Some(cookie) = response.cookie {
        head.push_str(&format!("Set-Cookie: {cookie}\r\n"));
    }
    head.push_str("\r\n");
    let _ = stream.write_all(head.as_bytes()).await;
    let _ = stream.write_all(&response.body).await;
    let _ = stream.shutdown().await;
    // Lingering close: discard what a rejected request still sends, so the
    // close is not a reset that destroys the response before it is read.
    let mut sink = [0u8; 4096];
    let mut left: usize = 64 * 1024;
    while left > 0 {
        match tokio::time::timeout(Duration::from_secs(1), stream.read(&mut sink)).await {
            Ok(Ok(n)) if n > 0 => left = left.saturating_sub(n),
            _ => break,
        }
    }
}

async fn connection(mut stream: TcpStream, peer: SocketAddr, shared: Arc<Shared>) {
    let request = match tokio::time::timeout(READ_TIMEOUT, read_request(&mut stream)).await {
        Ok(Ok(request)) => request,
        Ok(Err(status)) => return write_response(&mut stream, Response::json(status, json!({"version": VERSION, "status": "invalid-request"}))).await,
        Err(_) => return,
    };
    let response = handle(&shared, peer, &request).await;
    write_response(&mut stream, response).await;
}

const INDEX_HTML: &str = include_str!("console/index.html");
const CONSOLE_JS: &str = include_str!("console/console.js");
const CONSOLE_CSS: &str = include_str!("console/console.css");

async fn handle(shared: &Shared, peer: SocketAddr, request: &Request) -> Response {
    // DNS rebinding: a page on another name resolving to 127.0.0.1 sends its own Host.
    let host_ok = request.headers.get("host").is_some_and(|h| shared.authorities.iter().any(|a| a.eq_ignore_ascii_case(h)));
    if !host_ok {
        return Response::json(421, json!({"version": VERSION, "status": "invalid-host"}));
    }
    let asset = |content_type, body: &str| Response { status: 200, content_type, body: body.as_bytes().to_vec(), cookie: None };
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/") => asset("text/html; charset=utf-8", INDEX_HTML),
        ("GET", "/console.js") => asset("text/javascript; charset=utf-8", CONSOLE_JS),
        ("GET", "/console.css") => asset("text/css; charset=utf-8", CONSOLE_CSS),
        ("POST", "/api/v1/session") => login(shared, peer, request),
        ("POST", "/api/v1/logout") => logout(shared, peer, request),
        ("POST", "/api/v1/request") => api(shared, peer, request).await,
        (_, "/" | "/console.js" | "/console.css" | "/api/v1/session" | "/api/v1/logout" | "/api/v1/request") =>
            Response::json(405, json!({"version": VERSION, "status": "invalid-request"})),
        _ => Response::outcome("not-found"),
    }
}

/// Origin and fetch-metadata checks; cookie-authenticated calls require Origin.
fn origin_ok(shared: &Shared, request: &Request, required: bool) -> bool {
    let site_ok = request.headers.get("sec-fetch-site").is_none_or(|s| s == "same-origin" || s == "none");
    let origin = match request.headers.get("origin") {
        Some(origin) => shared.origins.iter().any(|o| o.eq_ignore_ascii_case(origin)),
        None => !required,
    };
    // JSON only: a cross-site form or simple request cannot send this type.
    let json_body = request.headers.get("content-type").is_some_and(|t| {
        let t = t.to_ascii_lowercase();
        t == "application/json" || t == "application/json; charset=utf-8"
    });
    site_ok && origin && json_body
}

fn now_ms() -> u64 {
    crate::telemetry::now_ms()
}

fn audit(shared: &Shared, peer: SocketAddr, user: Option<&str>, operation: &str, component: Option<&str>, status: &str) -> anyhow::Result<()> {
    let operation = if GRANTS.contains(&operation) || ["login", "logout", "request"].contains(&operation) { operation } else { "other" };
    let component = component.filter(|c| crate::deps::valid_name(c));
    let result = shared.audit.record(&json!({"ts_ms": now_ms(), "peer": peer.to_string(), "user": user,
        "operation": operation, "component": component, "status": status}), status == "requested");
    if let Err(error) = &result {
        warn!(%error, "Console audit record failed");
    }
    result
}

fn unexpired(user: &User) -> bool {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(u64::MAX);
    user.expires_unix.is_none_or(|e| now < e)
}

/// The user a bearer token names. Only generated 256-bit tokens are accepted,
/// so guessing is infeasible; no lockout, which would let any local process
/// deny the operator access.
fn bearer(shared: &Shared, value: &str) -> Result<String, &'static str> {
    let presented = value.strip_prefix("Bearer ").and_then(token_hash).ok_or("unauthenticated")?;
    let mut found = None;
    for (name, user) in &shared.settings.users {
        let configured = hex::decode(&user.token_sha256).unwrap_or_default();
        if equal(&configured, &presented) && unexpired(user) {
            found = Some(name.clone());
        }
    }
    found.ok_or("unauthenticated")
}

fn session_id(request: &Request) -> Option<[u8; 32]> {
    let cookies = request.headers.get("cookie")?;
    let mut found = None;
    for pair in cookies.split(';') {
        if let Some(value) = pair.trim().strip_prefix(&format!("{COOKIE}=")) {
            if found.is_some() { return None; } // Ambiguous duplicate.
            found = Some(Sha256::digest(value.as_bytes()).into());
        }
    }
    found
}

/// Authenticate: a bearer token, or a live session plus its CSRF token.
/// Returns the user name and whether a session (cookie) was used.
fn authenticate(shared: &Shared, request: &Request) -> Result<(String, Option<[u8; 32]>), &'static str> {
    let session = session_id(request);
    match (request.headers.get("authorization"), session) {
        (Some(_), Some(_)) => Err("unauthenticated"),
        (Some(value), None) => bearer(shared, value).map(|u| (u, None)),
        (None, Some(id)) => {
            let mut sessions = shared.sessions.lock().unwrap();
            let settings = &shared.settings;
            sessions.retain(|_, s| s.last.elapsed().as_secs() < settings.session_idle_seconds
                && s.created.elapsed().as_secs() < settings.session_max_seconds && settings.users.get(&s.user).is_some_and(unexpired));
            let session = sessions.get_mut(&id).ok_or("unauthenticated")?;
            let csrf = request.headers.get("x-suifleet-csrf").map(String::as_bytes).unwrap_or_default();
            if !equal(csrf, session.csrf.as_bytes()) {
                return Err("unauthenticated");
            }
            session.last = Instant::now();
            Ok((session.user.clone(), Some(id)))
        }
        (None, None) => Err("unauthenticated"),
    }
}

fn login(shared: &Shared, peer: SocketAddr, request: &Request) -> Response {
    // Login CSRF: a cross-site page cannot log a browser into a session either.
    if !origin_ok(shared, request, false) {
        let _ = audit(shared, peer, None, "login", None, "invalid-origin");
        return Response::outcome("invalid-origin");
    }
    let user = match request.headers.get("authorization").ok_or("unauthenticated").and_then(|v| bearer(shared, v)) {
        Ok(user) => user,
        Err(status) => {
            let _ = audit(shared, peer, None, "login", None, status);
            return Response::outcome(status);
        }
    };
    if audit(shared, peer, Some(&user), "login", None, "ok").is_err() {
        return Response::outcome("unavailable");
    }
    let secret = random_hex();
    let csrf = random_hex();
    let mut sessions = shared.sessions.lock().unwrap();
    if sessions.len() >= MAX_SESSIONS {
        // Least recently used first; every session is from a valid token holder.
        if let Some(oldest) = sessions.iter().min_by_key(|(_, s)| s.last).map(|(k, _)| *k) {
            sessions.remove(&oldest);
        }
    }
    let now = Instant::now();
    sessions.insert(Sha256::digest(secret.as_bytes()).into(), Session { user: user.clone(), csrf: csrf.clone(), created: now, last: now });
    let grants = &shared.settings.users[&user].grants;
    let mut response = Response::json(200, json!({"version": VERSION, "status": "ok", "data": {
        "user": user, "grants": grants, "csrf": csrf,
        "idle_seconds": shared.settings.session_idle_seconds, "max_seconds": shared.settings.session_max_seconds}}));
    response.cookie = Some(format!("{COOKIE}={secret}; HttpOnly; SameSite=Strict; Path=/api/v1; Max-Age={}", shared.settings.session_max_seconds));
    response
}

fn logout(shared: &Shared, peer: SocketAddr, request: &Request) -> Response {
    if !origin_ok(shared, request, true) {
        return Response::outcome("invalid-origin");
    }
    match authenticate(shared, request) {
        Ok((user, Some(id))) => {
            shared.sessions.lock().unwrap().remove(&id);
            let _ = audit(shared, peer, Some(&user), "logout", None, "ok");
            let mut response = Response::outcome("ok");
            response.cookie = Some(format!("{COOKIE}=; HttpOnly; SameSite=Strict; Path=/api/v1; Max-Age=0"));
            response
        }
        Ok(_) | Err(_) => Response::outcome("unauthenticated"),
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Call {
    #[allow(dead_code)]
    version: u64,
    operation: String,
    #[serde(default)]
    component: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    after: Option<String>,
}

async fn api(shared: &Shared, peer: SocketAddr, request: &Request) -> Response {
    let auth = authenticate(shared, request);
    let cookie = matches!(auth, Ok((_, Some(_))));
    let origin = origin_ok(shared, request, cookie);
    let body: Value = serde_json::from_slice(&request.body).unwrap_or(Value::Null);
    let operation = body.get("operation").and_then(Value::as_str).unwrap_or_default().to_owned();
    let component = body.get("component").and_then(Value::as_str).map(str::to_owned);
    let (operation, component) = (operation.as_str(), component.as_deref());
    let (user, grants) = match &auth {
        Ok((user, _)) => (Some(user.as_str()), shared.settings.users[user].grants.clone()),
        Err(_) => (None, vec![]),
    };
    if let Err(status) = decide(body.get("version").unwrap_or(&Value::Null), auth.is_ok(), origin, operation, &grants) {
        let _ = audit(shared, peer, user, operation, component, status);
        return Response::outcome(status);
    }
    let user = user.unwrap_or_default();
    let call: Call = match serde_json::from_value(body) {
        Ok(call) => call,
        Err(_) => {
            let _ = audit(shared, peer, Some(user), operation, component, "invalid-request");
            return Response::outcome("invalid-request");
        }
    };
    let support = SUPPORT.contains(&call.operation.as_str());
    if support {
        // Fail closed: no support operation runs without its audit record.
        if audit(shared, peer, Some(user), &call.operation, call.component.as_deref(), "requested").is_err() {
            return Response::outcome("unavailable");
        }
    }
    let (status, data) = match operate(shared, &call).await {
        Ok(data) => ("ok", Some(data)),
        Err(status) => (status, None),
    };
    let _ = audit(shared, peer, Some(user), &call.operation, call.component.as_deref(), status);
    let Some(data) = data else { return Response::outcome(status) };
    let response = Response::json(200, json!({"version": VERSION, "status": "ok", "data": data}));
    if response.body.len() > MAX_RESPONSE {
        return Response::outcome("too-large");
    }
    response
}

fn component_name(call: &Call) -> Result<&str, &'static str> {
    call.component.as_deref().filter(|c| crate::deps::valid_name(c)).ok_or("invalid-request")
}

fn page(call: &Call, default: usize) -> Result<usize, &'static str> {
    match call.limit {
        None => Ok(default),
        Some(limit) if (1..=MAX_PAGE).contains(&limit) => Ok(limit),
        Some(_) => Err("invalid-request"),
    }
}

async fn operate(shared: &Shared, call: &Call) -> Result<Value, &'static str> {
    let state_dir = &shared.context.state_dir;
    match call.operation.as_str() {
        "inventory" => {
            let limit = page(call, MAX_PAGE)?;
            let inventory = crate::telemetry::inventory(state_dir);
            let mut components = inventory.components.iter()
                .filter(|c| call.after.as_ref().is_none_or(|after| &c.name > after)).peekable();
            let page: Vec<_> = components.by_ref().take(limit).collect();
            // The cursor is the raw name: a display name may be redacted.
            let next = components.peek().and(page.last()).map(|c| c.name.clone());
            let listed: Vec<Value> = page.into_iter().map(|c| {
                let fields = json!({"component": bounded(&c.name, 128), "version": bounded(&c.version, 128),
                    "health": c.phase.clone().unwrap_or_else(|| if c.installed { "installed".into() } else { "unconfirmed".into() }),
                    "deployment": bounded(&c.deployment_id, 128), "installed": c.installed, "supervised": c.supervised,
                    "restarts": c.restarts, "since_ms": c.since_ms, "last_exit": c.last_exit.as_deref().map(|e| bounded(e, 256))});
                Value::Object(allow(fields.as_object().unwrap()))
            }).collect();
            Ok(json!({"daemon_version": env!("CARGO_PKG_VERSION"), "components": listed, "next": next}))
        }
        "health" => {
            let now = now_ms();
            let sync = shared.context.sync.lock().unwrap().clone();
            let inventory = crate::telemetry::inventory(state_dir);
            let mut reasons = crate::telemetry::sync_reasons(&sync, shared.context.poll_seconds, now);
            reasons.extend(inventory.reasons);
            let more = reasons.len().saturating_sub(MAX_REASONS);
            let mut reasons: Vec<String> = reasons.iter().take(MAX_REASONS).map(|r| bounded(r, 256)).collect();
            if more > 0 { reasons.push(format!("{more} more problems")); }
            Ok(json!({"health": if reasons.is_empty() { "healthy" } else { "degraded" }, "reasons": reasons,
                "daemon_version": env!("CARGO_PKG_VERSION"), "started_ms": shared.context.started_ms,
                "uptime_ms": now.saturating_sub(shared.context.started_ms), "pending_receipts": inventory.pending_receipts,
                "components": inventory.components.len(),
                "sync": {"last_success_ms": sync.last_success_ms, "consecutive_failures": sync.consecutive_failures}}))
        }
        "deployments" => {
            let limit = page(call, 20)?;
            let journal: crate::state::State = std::fs::read(state_dir.join("state.json")).ok()
                .and_then(|b| serde_json::from_slice(&b).ok()).ok_or("unavailable")?;
            let mut entries = journal.deployments.iter().filter(|(id, _)| call.after.as_ref().is_none_or(|after| *id > after)).peekable();
            let page: Vec<_> = entries.by_ref().take(limit).collect();
            let next = entries.peek().and(page.last()).map(|(id, _)| (*id).clone());
            let listed: Vec<Value> = page.into_iter().map(|(id, o)| json!({
                "deployment": bounded(id, 128), "status": o.status, "reported": o.reported, "indeterminate": o.indeterminate,
                "error": o.error.as_deref().map(|e| bounded(e, 256))})).collect();
            Ok(json!({"deployments": listed, "next": next}))
        }
        "logs" => {
            let name = component_name(call)?;
            let lines = match call.limit { None => 50, Some(n) if (1..=MAX_LOG_LINES).contains(&n) => n, Some(_) => return Err("invalid-request") };
            if !crate::telemetry::inventory(state_dir).components.iter().any(|c| c.name == name && c.supervised) {
                return Err("not-found");
            }
            let path = state_dir.join("supervised").join(name).join("component.log");
            let text = tail(&path).map_err(|_| "not-found")?;
            let mut all: Vec<&str> = text.lines().collect();
            let skip = all.len().saturating_sub(lines);
            let lines: Vec<String> = all.drain(skip..).map(|l| crate::telemetry::bounded(&redact_text(l), MAX_LINE)).collect();
            Ok(json!({"component": name, "lines": lines}))
        }
        "configuration" => {
            let name = component_name(call)?;
            let record = shared.configs.committed(name).map_err(|_| "unavailable")?.ok_or("not-found")?;
            let mut effective = Value::Object(record.effective);
            for pointer in &record.confidential {
                remove_pointer(&mut effective, pointer);
            }
            Ok(json!({"component": name, "version": bounded(&record.version, 128), "deployment": bounded(&record.deployment, 128),
                "effective": redact_value(&effective, 0)}))
        }
        "audit" => {
            let limit = page(call, 20)?;
            Ok(json!({"entries": shared.audit.tail(limit).map_err(|_| "unavailable")?}))
        }
        operation if SUPPORT.contains(&operation) => {
            if !shared.settings.support_operations.iter().any(|op| op == operation) {
                return Err("denied");
            }
            let name = component_name(call)?;
            let (reply, receiver) = oneshot::channel();
            shared.controls.try_send(Control { operation: operation.into(), component: name.into(), reply }).map_err(|_| "busy")?;
            match tokio::time::timeout(CONTROL_TIMEOUT, receiver).await {
                Ok(Ok(Ok(runtime))) => Ok(json!({"component": name, "runtime": redact_value(&runtime, 0)})),
                Ok(Ok(Err(error))) => {
                    warn!(operation, component = name, %error, "Console support operation failed");
                    Err("failed")
                }
                // Dropped by the loop, or still queued: whether it ran is unknown.
                Ok(Err(_)) | Err(_) => Err("unknown"),
            }
        }
        _ => Err("invalid-request"),
    }
}

fn tail(path: &Path) -> std::io::Result<String> {
    let mut file = std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(path)?;
    let size = file.metadata()?.len();
    file.seek(SeekFrom::Start(size.saturating_sub(MAX_LOG_BYTES)))?;
    let mut bytes = Vec::new();
    file.take(MAX_LOG_BYTES).read_to_end(&mut bytes)?;
    let mut text = String::from_utf8_lossy(&bytes).into_owned();
    if size > MAX_LOG_BYTES {
        // The first line is cut; drop it.
        text = text.split_once('\n').map(|(_, rest)| rest.to_owned()).unwrap_or_default();
    }
    Ok(text)
}

fn remove_pointer(value: &mut Value, pointer: &str) {
    let Some((parent, last)) = pointer.rsplit_once('/') else { return };
    let last = last.replace("~1", "/").replace("~0", "~");
    if let Some(target) = value.pointer_mut(parent) {
        match target {
            Value::Object(map) => { map.remove(&last); }
            Value::Array(items) => if let Ok(i) = last.parse::<usize>() { if i < items.len() { items[i] = json!(REDACTED); } },
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn settings() -> Settings {
        let (_, hash) = new_token();
        toml::from_str(&format!("users.ops = {{ token_sha256 = \"{hash}\", grants = [\"inventory\"] }}")).unwrap()
    }

    #[test]
    fn configuration_is_loopback_only_and_strict() {
        let s = settings();
        s.validate().unwrap();
        assert_eq!(s.listen, "127.0.0.1:1441".parse().unwrap());
        assert_eq!(s.allowed_origins(), ["http://127.0.0.1:1441", "http://localhost:1441"]);
        for listen in ["0.0.0.0:1441", "192.168.1.5:1441", "[::]:1441", "127.0.0.1:0"] {
            assert!(Settings { listen: listen.parse().unwrap(), ..s.clone() }.validate().is_err(), "{listen}");
        }
        assert!(Settings { listen: "[::1]:1441".parse().unwrap(), ..s.clone() }.validate().is_ok());
        for origin in ["https://127.0.0.1:1441", "http://example.com:1441", "http://127.0.0.1", "http://10.0.0.1:1441", "http://localhost:0"] {
            assert!(Settings { origins: vec![origin.into()], ..s.clone() }.validate().is_err(), "{origin}");
        }
        assert!(Settings { origins: vec!["http://localhost:9000".into()], ..s.clone() }.validate().is_ok());
        let mut bad = s.clone();
        bad.users.get_mut("ops").unwrap().grants = vec!["restart".into()];
        assert!(bad.validate().is_err(), "support grant without an enabled operation");
        bad.support_operations = vec!["restart".into()];
        bad.validate().unwrap();
        bad.users.get_mut("ops").unwrap().grants.push("shell".into());
        assert!(bad.validate().is_err());
        let mut bad = s.clone();
        bad.users.get_mut("ops").unwrap().token_sha256 = "A".repeat(64);
        assert!(bad.validate().is_err());
        let mut bad = s.clone();
        let duplicate = bad.users["ops"].clone();
        bad.users.insert("other".into(), duplicate);
        assert!(bad.validate().is_err(), "shared token");
        assert!(Settings { users: BTreeMap::new(), ..s.clone() }.validate().is_err());
        assert!(toml::from_str::<Settings>("unknown = 1\nusers = {}").is_err());
    }

    #[test]
    fn tokens_are_generated_hashed_and_compared_exactly() {
        let (token, hash) = new_token();
        assert_eq!(hex::encode(token_hash(&token).unwrap()), hash);
        assert!(token_hash("hunter2").is_none());
        assert!(token_hash(&token.to_uppercase()).is_none());
        assert!(token_hash(&token[..token.len() - 1]).is_none());
        assert!(equal(b"abc", b"abc") && !equal(b"abc", b"abd") && !equal(b"abc", b"ab"));
    }

    #[test]
    fn decisions_follow_the_v1_reference_contract() {
        // SF-064 console vectors (interfaces/compat/remaining-roadmap-cases.json)
        // applied to this implementation's decision and field allowlist.
        let cases: Value = serde_json::from_str(include_str!("../../interfaces/compat/remaining-roadmap-cases.json")).unwrap();
        let mut checked = 0;
        for case in cases["cases"].as_array().unwrap().iter().filter(|c| c["scope"] == "console") {
            let r = &case["request"];
            let grants: Vec<String> = serde_json::from_value(r["grants"].clone()).unwrap();
            let decision = decide(&r["version"], r["authorized"] == true, r["origin"] == r["expected_origin"],
                r["operation"].as_str().unwrap(), &grants);
            let actual = match decision {
                // The reference's "authorized: false" is an unauthenticated caller here.
                Err("unauthenticated") => json!({"status": "denied"}),
                Err(status) => json!({"status": status}),
                Ok(()) => json!({"status": "ok", "fields": allow(r["fields"].as_object().unwrap())}),
            };
            assert_eq!(actual, case["expected"], "{}", case["id"]);
            checked += 1;
        }
        assert!(checked >= 4);
        assert_eq!(decide(&json!(true), true, true, "inventory", &["inventory".into()]), Err("unsupported-version"));
        assert_eq!(decide(&json!(1.0), true, true, "inventory", &["inventory".into()]), Err("unsupported-version"));
        assert_eq!(decide(&json!(1), true, true, "restart", &["inventory".into()]), Err("denied"));
    }

    #[test]
    fn redaction_removes_secrets_and_keeps_diagnostics() {
        let (token, _) = new_token();
        let key = "07".repeat(32);
        // Built at run time: a literal would be (rightly) flagged by secret scanning.
        let access_key = format!("AKIA{}", "ABCDEFGHIJKLMNOP");
        let text = format!("started pid 7; password=hunter2 api_key: abc123 \"secret\": \"s3\" Authorization: Bearer xyz.uvw \
            token {token} {access_key} suiprivkey1qqqq key {key} deployment 0x{key} url=https://example.com/a -----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY----- done");
        let out = redact_text(&text);
        for secret in ["hunter2", "abc123", "s3\"", "xyz.uvw", token.as_str(), access_key.as_str(), "suiprivkey1qqqq", "MIIE"] {
            assert!(!out.contains(secret), "{secret} leaked: {out}");
        }
        assert!(!out.contains(&format!(" {key} ")), "{out}");
        for kept in ["started pid 7", "url=https://example.com/a", "done", &format!("0x{key}")] {
            assert!(out.contains(kept), "{kept} lost: {out}");
        }
        let value = json!({"name": "svc", "nested": {"privateKey": "x", "list": [{"aws_secret_access_key": "y"}], "note": "pin=1234"}, "port": 80});
        let out = redact_value(&value, 0);
        assert_eq!(out["name"], "svc");
        assert_eq!(out["port"], 80);
        assert_eq!(out["nested"]["privateKey"], REDACTED);
        assert_eq!(out["nested"]["list"][0]["aws_secret_access_key"], REDACTED);
        assert!(!out.to_string().contains("1234"));
        let mut deep = json!("x");
        for _ in 0..40 { deep = json!([deep]); }
        assert!(redact_value(&deep, 0).to_string().contains("[truncated]"));
        let fields = json!({"component": "demo", "version": "1.0", "health": "ok", "key": "k", "token": "t", "env": "x"});
        assert_eq!(Value::Object(allow(fields.as_object().unwrap())), json!({"component": "demo", "version": "1.0", "health": "ok"}));
    }

    #[test]
    fn ui_assets_satisfy_the_content_security_policy() {
        // No inline script/style/handlers (blocked by the CSP) and no HTML
        // sinks: device data is only ever rendered as text.
        assert!(!INDEX_HTML.contains("<script>") && !INDEX_HTML.contains("<style") && !INDEX_HTML.contains(" on"));
        assert!(!INDEX_HTML.contains("style="));
        for sink in ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(", "localStorage", "sessionStorage"] {
            assert!(!CONSOLE_JS.contains(sink), "{sink}");
        }
        assert!(CONSOLE_JS.contains("'X-SuiFleet-CSRF'") && CONSOLE_JS.contains("credentials: 'same-origin'"));
    }

    #[test]
    fn confidential_pointers_are_removed() {
        let mut value = json!({"a": {"b/c": 1, "d": 2}, "list": [1, 2]});
        remove_pointer(&mut value, "/a/b~1c");
        remove_pointer(&mut value, "/list/1");
        remove_pointer(&mut value, "/missing/x");
        assert_eq!(value, json!({"a": {"d": 2}, "list": [1, REDACTED]}));
    }

    #[test]
    fn audit_is_private_bounded_and_ordered() {
        use std::os::unix::fs::PermissionsExt;
        let dir = std::env::temp_dir().join(format!("suifleet-console-{}", rand::random::<u128>()));
        let audit = Audit::open(&dir, 64 * 1024).unwrap();
        for i in 0..2000 {
            audit.record(&json!({"i": i, "pad": "x".repeat(64)}), i % 100 == 0).unwrap();
        }
        let path = dir.join("console/audit.jsonl");
        assert!(std::fs::metadata(&path).unwrap().len() <= 64 * 1024);
        assert!(dir.join("console/audit.jsonl.1").exists());
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(std::fs::metadata(dir.join("console")).unwrap().permissions().mode() & 0o777, 0o700);
        let tail = audit.tail(5).unwrap();
        assert_eq!(tail.iter().map(|e| e["i"].as_u64().unwrap()).collect::<Vec<_>>(), [1995, 1996, 1997, 1998, 1999]);
        // A symlinked audit path is refused, never followed.
        std::fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink("/dev/null", &path).unwrap();
        assert!(audit.record(&json!({}), true).is_err());
        std::fs::remove_dir_all(dir).unwrap();
    }
}
