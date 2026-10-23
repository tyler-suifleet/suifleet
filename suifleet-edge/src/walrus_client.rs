use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

use crate::config::WalrusConfig;
use futures_util::StreamExt;
use sha2::{Digest, Sha256};
use tracing::{info, warn};

const MAX_RECIPE_BYTES: usize = 1024 * 1024;

#[derive(Clone)]
pub struct WalrusClient {
    aggregator_urls: Vec<String>,
    range_chunk_bytes: u64,
    /// Seal decryption for confidential blobs (SF-022), when configured.
    seal: Option<std::sync::Arc<crate::seal_client::Seal>>,
    aggregator_url: String,
    http: reqwest::Client,
    max_artifact_bytes: u64,
    min_free_bytes: u64,
    attempts: u32,
    backoff: Duration,
    deadline: Duration,
}

/// Why a transfer attempt stopped.
enum Failure {
    /// Retrying cannot help (expired/unknown blob, limits, local disk).
    Permanent(anyhow::Error),
    /// Connection drop, timeout, 5xx/429: retry, resuming any partial file.
    Transient(anyhow::Error),
}

impl WalrusClient {
    pub fn new(config: &WalrusConfig) -> Self {
        Self {
            seal: None,
            aggregator_urls: if config.aggregator_urls.is_empty() {
                vec![config.aggregator_url.clone()]
            } else {
                config.aggregator_urls.clone()
            },
            range_chunk_bytes: config.range_chunk_bytes.clamp(1, 10 * 1024 * 1024),
            aggregator_url: config.aggregator_url.trim_end_matches('/').to_owned(),
            http: reqwest::Client::builder()
                .connect_timeout(Duration::from_secs(30))
                .read_timeout(Duration::from_secs(60))
                .build()
                .expect("HTTP client"),
            max_artifact_bytes: config.max_artifact_bytes,
            min_free_bytes: config.min_free_bytes,
            attempts: config.download_attempts.max(1),
            backoff: Duration::from_secs(1),
            deadline: Duration::from_secs(config.download_timeout_seconds),
        }
    }

    /// Reserve a bounded share of the blob deadline for each ordered mirror.
    pub fn mirror_deadline(&self) -> Duration {
        self.deadline / u32::try_from(self.aggregator_urls.len().max(1)).unwrap_or(u32::MAX)
    }

    /// One client per manifest mirror, preserving operator order.
    pub fn mirrors(&self) -> impl Iterator<Item = Self> + '_ {
        self.aggregator_urls.iter().map(|url| {
            let mut client = self.clone();
            client.aggregator_url = url.trim_end_matches('/').to_owned();
            client
        })
    }

    pub fn deadline(&self) -> Duration {
        self.deadline
    }

    pub fn discard_partial(dest: &Path) -> anyhow::Result<()> {
        match std::fs::remove_file(partial_path(dest)) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }

    pub fn with_seal(mut self, seal: Option<std::sync::Arc<crate::seal_client::Seal>>) -> Self {
        self.seal = seal;
        self
    }

    /// Whether only Seal-encrypted recipes and artifacts may be applied.
    pub fn confidential_only(&self) -> bool {
        self.seal.as_ref().is_some_and(|seal| seal.required())
    }

    /// Decrypt a confidential blob (`MAGIC || EncryptedObject`) in memory and
    /// check its plaintext digest; returns the plaintext. Nothing is written.
    pub async fn open(
        &self,
        mut blob: Vec<u8>,
        kind: crate::seal::Kind,
        sha256: &str,
    ) -> anyhow::Result<zeroize::Zeroizing<Vec<u8>>> {
        let seal = self.seal.as_ref().ok_or_else(|| {
            anyhow::anyhow!(
                "this {} is Seal-encrypted, but confidential delivery is not configured ([seal])",
                kind.as_str()
            )
        })?;
        anyhow::ensure!(
            blob.len() as u64 <= seal.max_plaintext_bytes() + 64 * 1024,
            "confidential {} exceeds the {} byte decryption limit",
            kind.as_str(),
            seal.max_plaintext_bytes()
        );
        let range = seal.decrypt(&mut blob, kind, sha256).await;
        let range = match range {
            Ok(range) => range,
            Err(error) => {
                zeroize::Zeroize::zeroize(&mut blob);
                return Err(error);
            }
        };
        let mut blob = zeroize::Zeroizing::new(blob);
        blob.copy_within(range.clone(), 0);
        blob.truncate(range.len());
        anyhow::ensure!(
            hex::encode(Sha256::digest(blob.as_slice())) == sha256.to_ascii_lowercase(),
            "decrypted {} SHA-256 differs from the on-chain digest",
            kind.as_str()
        );
        Ok(blob)
    }

    fn url(&self, blob_id: &str) -> String {
        format!("{}/v1/blobs/{}", self.aggregator_url, blob_id)
    }

    /// Raw recipe bytes; callers authenticate them against a SHA-256 digest.
    pub async fn recipe_bytes(&self, blob_id: &str) -> anyhow::Result<Vec<u8>> {
        tokio::time::timeout(self.deadline, async {
            let mut error = anyhow::anyhow!("no Walrus mirrors configured");
            for mirror in self.mirrors() {
                match tokio::time::timeout(
                    self.mirror_deadline(),
                    mirror.recipe_from_mirror(blob_id),
                )
                .await
                {
                    Ok(Ok(bytes)) => return Ok(bytes),
                    Ok(Err(e)) => error = e,
                    Err(_) => error = anyhow::anyhow!("recipe mirror deadline exceeded"),
                }
            }
            Err(error)
        })
        .await
        .map_err(|_| anyhow::anyhow!("recipe mirror deadline exceeded"))?
    }

    async fn recipe_from_mirror(&self, blob_id: &str) -> anyhow::Result<Vec<u8>> {
        let bytes = self
            .retry(blob_id, || async {
                let response = self.get(blob_id, None).await?;
                let mut stream = response.bytes_stream();
                let mut bytes = Vec::new();
                while let Some(chunk) = stream.next().await {
                    let chunk = chunk.map_err(|e| Failure::Transient(e.into()))?;
                    if bytes.len() + chunk.len() > MAX_RECIPE_BYTES {
                        return Err(Failure::Permanent(anyhow::anyhow!(
                            "recipe exceeds 1 MiB limit"
                        )));
                    }
                    bytes.extend_from_slice(&chunk);
                }
                Ok(bytes)
            })
            .await?;
        Ok(bytes)
    }

    /// Download a blob to `dest` and return the SHA-256 of the complete file.
    ///
    /// Bytes land in `<dest>.partial` and are renamed only once the transfer
    /// completes; callers still verify the digest before applying anything.
    /// Interrupted transfers resume with HTTP Range (or restart if the
    /// aggregator ignores it) up to `download_attempts` times, all within
    /// `download_timeout_seconds`.
    pub async fn download(&self, blob_id: &str, dest: &Path) -> anyhow::Result<String> {
        let partial = partial_path(dest);
        info!("Downloading blob {} from {}", blob_id, self.url(blob_id));
        self.retry(blob_id, || self.transfer(blob_id, &partial))
            .await?;
        let hash = sha256_file(&partial)?;
        std::fs::rename(&partial, dest)?;
        info!(
            "Downloaded {} bytes, sha256={}",
            std::fs::metadata(dest)?.len(),
            hash
        );
        Ok(hash)
    }

    /// Run `attempt` until it succeeds, fails permanently, exhausts the
    /// attempts or overruns the deadline. `read_timeout` alone cannot bound a
    /// transfer: an aggregator trickling a byte a minute never trips it.
    async fn retry<T, F, Fut>(&self, blob_id: &str, attempt: F) -> anyhow::Result<T>
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<T, Failure>>,
    {
        tokio::time::timeout(self.deadline, self.attempts(blob_id, attempt))
            .await
            .unwrap_or_else(|_| {
                Err(anyhow::anyhow!(
                    "blob {blob_id}: transfer did not finish within {}s",
                    self.deadline.as_secs()
                ))
            })
    }

    async fn attempts<T, F, Fut>(&self, blob_id: &str, mut attempt: F) -> anyhow::Result<T>
    where
        F: FnMut() -> Fut,
        Fut: std::future::Future<Output = Result<T, Failure>>,
    {
        let mut delay = self.backoff;
        for number in 1..=self.attempts {
            match attempt().await {
                Ok(value) => return Ok(value),
                Err(Failure::Permanent(error)) => return Err(error),
                Err(Failure::Transient(error)) if number == self.attempts => {
                    return Err(error.context(format!(
                        "blob {blob_id}: transfer failed after {number} attempts"
                    )))
                }
                Err(Failure::Transient(error)) => {
                    warn!(
                        blob = blob_id,
                        attempt = number,
                        "transfer interrupted: {error:#}; retrying"
                    );
                    tokio::time::sleep(delay).await;
                    delay = (delay * 2).min(Duration::from_secs(30));
                }
            }
        }
        unreachable!("attempts is at least one")
    }

    async fn get(&self, blob_id: &str, offset: Option<u64>) -> Result<reqwest::Response, Failure> {
        let mut request = self
            .http
            .get(self.url(blob_id))
            .header(reqwest::header::ACCEPT_ENCODING, "identity");
        if let Some(offset) = offset {
            request = request.header(
                reqwest::header::RANGE,
                format!(
                    "bytes={offset}-{}",
                    offset.saturating_add(self.range_chunk_bytes - 1)
                ),
            );
        }
        let response = request
            .send()
            .await
            .map_err(|e| Failure::Transient(e.into()))?;
        let status = response.status();
        if status.is_success() {
            return Ok(response);
        }
        let error = anyhow::anyhow!("aggregator returned {status} for blob {blob_id}");
        Err(match status.as_u16() {
            404 | 410 => Failure::Permanent(anyhow::anyhow!(
                "blob {blob_id} is unavailable or expired on the aggregator ({status}); \
                 its storage period may have ended: re-upload and redeploy"
            )),
            408 | 429 | 500..=599 => Failure::Transient(error),
            _ => Failure::Permanent(error),
        })
    }

    async fn transfer(&self, blob_id: &str, partial: &Path) -> Result<(), Failure> {
        loop {
            let local = |e: std::io::Error| Failure::Permanent(e.into());
            let mut offset = std::fs::metadata(partial).map(|m| m.len()).unwrap_or(0);
            let response = match self.get(blob_id, Some(offset)).await {
                // Some servers reject a range starting at the exact end; restart cleanly.
                Err(Failure::Permanent(_)) if offset > 0 => {
                    offset = 0;
                    std::fs::remove_file(partial).map_err(local)?;
                    self.get(blob_id, Some(0)).await?
                }
                other => other?,
            };
            let resumed = response.status() == reqwest::StatusCode::PARTIAL_CONTENT;
            if offset > 0 && !resumed {
                offset = 0; // Range ignored: the body is the whole blob.
            }
            let range_end = offset.saturating_add(self.range_chunk_bytes - 1);
            let (expected_total, expected_end) = if resumed {
                let range = response
                    .headers()
                    .get(reqwest::header::CONTENT_RANGE)
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or_default();
                let parsed = range
                    .strip_prefix("bytes ")
                    .and_then(|v| v.split_once('/'))
                    .and_then(|(r, total)| r.split_once('-').map(|(a, b)| (a, b, total)))
                    .and_then(|(a, b, t)| {
                        Some((
                            a.parse::<u64>().ok()?,
                            b.parse::<u64>().ok()?,
                            t.parse::<u64>().ok()?,
                        ))
                    });
                let Some((start, end, total)) = parsed else {
                    return Err(Failure::Permanent(anyhow::anyhow!(
                        "invalid Content-Range {range:?}"
                    )));
                };
                if start != offset || end < start || end > range_end || end >= total {
                    return Err(Failure::Permanent(anyhow::anyhow!("aggregator resumed at the wrong offset or invalid range ({range:?}, expected {offset})")));
                }
                (Some(total), Some(end + 1))
            } else {
                // Legacy endpoints may ignore Range. Only accept a complete small response.
                if response
                    .content_length()
                    .is_some_and(|n| n > self.range_chunk_bytes)
                {
                    return Err(Failure::Permanent(anyhow::anyhow!(
                        "aggregator ignored bounded Range request"
                    )));
                }
                (response.content_length(), response.content_length())
            };
            let directory = partial.parent().unwrap_or(Path::new("."));
            if let Some(total) = expected_total {
                self.check_space(directory, total, offset)?;
            }
            // Bytes writable before free space must be checked again. Covers
            // chunked/unknown-length bodies and space consumed by other writers.
            let mut allowance = 0u64;
            let mut file = std::fs::OpenOptions::new()
                .create(true)
                .write(true)
                .append(resumed)
                .truncate(!resumed)
                .open(partial)
                .map_err(local)?;
            let mut length = offset;
            let mut stream = response.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.map_err(|e| Failure::Transient(e.into()))?;
                let size = chunk.len() as u64;
                length += size;
                let refusal = if length > expected_end.unwrap_or(self.range_chunk_bytes) {
                    Some(anyhow::anyhow!("response exceeds requested range"))
                } else if length > self.max_artifact_bytes {
                    Some(anyhow::anyhow!(
                        "artifact exceeds the {} byte download limit",
                        self.max_artifact_bytes
                    ))
                } else if size > allowance {
                    allowance = self.writable(directory)?;
                    (size > allowance).then(|| {
                        anyhow::anyhow!(
                        "insufficient disk space: {} bytes reserved, artifact stopped at {} bytes",
                        self.min_free_bytes,
                        length - size
                    )
                    })
                } else {
                    None
                };
                if let Some(error) = refusal {
                    drop(file);
                    let _ = std::fs::remove_file(partial);
                    return Err(Failure::Permanent(error));
                }
                allowance -= size;
                file.write_all(&chunk).map_err(local)?;
            }
            file.sync_all().map_err(local)?;
            if let Some(total) = expected_end {
                if length != total {
                    return Err(Failure::Transient(anyhow::anyhow!(
                        "transfer ended at {length} of {total} bytes"
                    )));
                }
            }
            if !resumed || expected_total == Some(length) {
                return Ok(());
            }
        }
    }

    /// Refuse a transfer that exceeds the artifact limit or would leave less
    /// than `min_free_bytes` free, before writing any of it.
    fn check_space(&self, directory: &Path, total: u64, offset: u64) -> Result<(), Failure> {
        if total > self.max_artifact_bytes {
            return Err(Failure::Permanent(anyhow::anyhow!(
                "artifact of {total} bytes exceeds the {} byte download limit",
                self.max_artifact_bytes
            )));
        }
        let available = available_bytes(directory).map_err(|e| Failure::Permanent(e.into()))?;
        let needed = total
            .saturating_sub(offset)
            .saturating_add(self.min_free_bytes);
        if available < needed {
            return Err(Failure::Permanent(anyhow::anyhow!(
                "insufficient disk space: {} bytes still to download plus {} reserved, {available} available",
                total - offset,
                self.min_free_bytes
            )));
        }
        Ok(())
    }

    /// The same limits for a local copy of `size` bytes into `directory`,
    /// such as a cached artifact, which never passes through `download`.
    pub fn ensure_room(&self, directory: &Path, size: u64) -> anyhow::Result<()> {
        self.check_space(directory, size, 0)
            .map_err(|(Failure::Permanent(e) | Failure::Transient(e))| e)
    }

    /// Bytes that can be written to `directory` while keeping `min_free_bytes`.
    fn writable(&self, directory: &Path) -> Result<u64, Failure> {
        let available = available_bytes(directory).map_err(|e| Failure::Permanent(e.into()))?;
        Ok(available.saturating_sub(self.min_free_bytes))
    }
}

fn partial_path(dest: &Path) -> PathBuf {
    let mut name = dest.file_name().unwrap_or_default().to_os_string();
    name.push(".partial");
    dest.with_file_name(name)
}

pub fn sha256_file(path: &Path) -> anyhow::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            return Ok(hex::encode(hasher.finalize()));
        }
        hasher.update(&buffer[..read]);
    }
}

fn available_bytes(directory: &Path) -> std::io::Result<u64> {
    let path = std::ffi::CString::new(directory.as_os_str().as_encoded_bytes())?;
    let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statvfs(path.as_ptr(), &mut stat) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(stat.f_bavail as u64 * stat.f_frsize as u64)
}

/// Remove artifact working directories left behind by earlier runs (for
/// example a crash mid-download). No transfer is in progress at startup, and
/// the journal never re-applies an interrupted deployment, so they are unused.
pub fn collect_garbage(artifacts: &Path) -> anyhow::Result<u64> {
    let entries = match std::fs::read_dir(artifacts) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(e) => return Err(e.into()),
    };
    let mut freed = 0;
    for entry in entries {
        let path = entry?.path();
        freed += directory_size(&path);
        if path.is_dir() {
            std::fs::remove_dir_all(&path)?;
        } else {
            std::fs::remove_file(&path)?;
        }
    }
    Ok(freed)
}

fn directory_size(path: &Path) -> u64 {
    match std::fs::symlink_metadata(path) {
        Ok(m) if m.is_dir() => std::fs::read_dir(path)
            .map(|d| {
                d.filter_map(|e| e.ok())
                    .map(|e| directory_size(&e.path()))
                    .sum()
            })
            .unwrap_or(0),
        Ok(m) => m.len(),
        Err(_) => 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    const BLOB: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";

    /// Scripted aggregator: `respond(request_number, range_offset)` returns raw
    /// response bytes; a trailing `None` body closes the connection early.
    async fn server<F>(respond: F) -> (String, Arc<Mutex<Vec<Option<u64>>>>)
    where
        F: Fn(usize, Option<u64>) -> Vec<u8> + Send + Sync + 'static,
    {
        raw_server(move |n, text| {
            let range = text
                .lines()
                .find_map(|l| l.strip_prefix("range: bytes="))
                .and_then(|r| r.split('-').next()?.parse().ok());
            respond(n, range)
        })
        .await
    }

    async fn raw_server<F>(respond: F) -> (String, Arc<Mutex<Vec<Option<u64>>>>)
    where
        F: Fn(usize, &str) -> Vec<u8> + Send + Sync + 'static,
    {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = seen.clone();
        let respond = Arc::new(respond);
        tokio::spawn(async move {
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = vec![0u8; 4096];
                let read = socket.read(&mut request).await.unwrap();
                let text = String::from_utf8_lossy(&request[..read]).to_lowercase();
                let range = text
                    .lines()
                    .find_map(|l| l.strip_prefix("range: bytes="))
                    .and_then(|r| r.split('-').next()?.parse().ok());
                let number = {
                    let mut seen = log.lock().unwrap();
                    seen.push(range);
                    seen.len()
                };
                let _ = socket.write_all(&respond(number, &text)).await;
                let _ = socket.shutdown().await;
            }
        });
        (format!("http://{address}"), seen)
    }

    fn ok(body: &[u8]) -> Vec<u8> {
        let mut out = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        )
        .into_bytes();
        out.extend_from_slice(body);
        out
    }

    fn partial(offset: usize) -> Vec<u8> {
        let body = &BLOB[offset..];
        let mut out = format!(
            "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {offset}-{}/{}\r\nConnection: close\r\n\r\n",
            body.len(), BLOB.len() - 1, BLOB.len()
        )
        .into_bytes();
        out.extend_from_slice(body);
        out
    }

    /// Promise the whole blob but send only the first `sent` bytes.
    fn truncated(sent: usize) -> Vec<u8> {
        let mut out = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            BLOB.len()
        )
        .into_bytes();
        out.extend_from_slice(&BLOB[..sent]);
        out
    }

    /// Chunked transfer encoding: no Content-Length, size unknown up front.
    fn chunked(body: &[u8]) -> Vec<u8> {
        let mut out = format!(
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:x}\r\n",
            body.len()
        )
        .into_bytes();
        out.extend_from_slice(body);
        out.extend_from_slice(b"\r\n0\r\n\r\n");
        out
    }

    fn status(code: &str) -> Vec<u8> {
        format!("HTTP/1.1 {code}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").into_bytes()
    }

    fn client(url: String, max: u64, free: u64) -> WalrusClient {
        let mut client = WalrusClient::new(&WalrusConfig {
            aggregator_url: url,
            max_artifact_bytes: max,
            min_free_bytes: free,
            download_attempts: 4,
            download_timeout_seconds: 3600,
            ..Default::default()
        });
        client.backoff = Duration::from_millis(10);
        client
    }

    fn dest(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("suifleet-walrus-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("artifact")
    }

    fn expected() -> String {
        hex::encode(Sha256::digest(BLOB))
    }

    #[tokio::test]
    async fn bounded_chunks_and_persisted_resume() {
        let (url, seen) = raw_server(|_, text| {
            let range = text.lines().find_map(|l| l.strip_prefix("range: bytes=")).unwrap();
            let (a,b) = range.split_once('-').unwrap();
            let a: usize = a.parse().unwrap(); let b: usize = b.parse().unwrap();
            assert_eq!(b - a + 1, 8);
            let b = b.min(BLOB.len()-1);
            let mut out = format!("HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {a}-{b}/{}\r\nConnection: close\r\n\r\n", b-a+1, BLOB.len()).into_bytes();
            out.extend_from_slice(&BLOB[a..=b]); out
        }).await;
        let dest = dest("bounded");
        std::fs::write(partial_path(&dest), &BLOB[..5]).unwrap();
        let mut client = client(url, 1 << 20, 0);
        client.range_chunk_bytes = 8;
        crate::components::artifact(&client, None, "blob", &expected(), &dest)
            .await
            .unwrap();
        assert_eq!(std::fs::read(&dest).unwrap(), BLOB);
        assert_eq!(
            *seen.lock().unwrap(),
            vec![Some(5), Some(13), Some(21), Some(29)]
        );
    }

    #[tokio::test]
    async fn artifact_falls_back_on_status_truncation_corruption_and_invalid_ranges() {
        for kind in [
            "missing",
            "server",
            "truncated",
            "corrupt",
            "bad-range",
            "ignored-range",
        ] {
            let (bad, _) = server(move |_, _| match kind {
                "missing" => status("404 Not Found"),
                "server" => status("503 Service Unavailable"),
                "truncated" => truncated(5),
                "corrupt" => ok(&[b'x'; BLOB.len()]),
                "bad-range" => partial(3),
                _ => ok(&[b'x'; 100]),
            })
            .await;
            let (good, seen) = server(|_, _| ok(BLOB)).await;
            let mut client = client(bad.clone(), 1 << 20, 0);
            client.aggregator_urls = vec![bad, good];
            client.range_chunk_bytes = 64;
            let dest = dest(kind);
            crate::components::artifact(&client, None, "blob", &expected(), &dest)
                .await
                .unwrap();
            assert_eq!(std::fs::read(&dest).unwrap(), BLOB, "{kind}");
            assert_eq!(*seen.lock().unwrap(), vec![Some(0)], "{kind}");
        }
    }

    #[tokio::test]
    async fn slow_first_mirror_leaves_time_for_fallback() {
        let (bad, _) = server(|_, _| status("503 Service Unavailable")).await;
        let (good, _) = server(|_, _| ok(BLOB)).await;
        let mut client = client(bad.clone(), 1 << 20, 0);
        client.aggregator_urls = vec![bad, good];
        client.attempts = 100;
        client.backoff = Duration::from_secs(10);
        client.deadline = Duration::from_secs(2);
        let dest = dest("mirror-deadline");
        crate::components::artifact(&client, None, "blob", &expected(), &dest)
            .await
            .unwrap();
        assert_eq!(std::fs::read(&dest).unwrap(), BLOB);
    }

    #[tokio::test]
    async fn all_corrupt_mirrors_leave_no_candidate() {
        let (bad, _) = server(|_, _| ok(b"wrong")).await;
        let mut client = client(bad.clone(), 1 << 20, 0);
        client.aggregator_urls = vec![bad.clone(), bad];
        let dest = dest("all-corrupt");
        let error = crate::components::artifact(&client, None, "blob", &expected(), &dest)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("SHA-256 mismatch"));
        assert!(!dest.exists());
        assert!(!partial_path(&dest).exists());
    }

    #[tokio::test]
    async fn interrupted_transfer_resumes_with_range() {
        let (url, seen) = server(|n, range| match (n, range) {
            (1, Some(0)) => truncated(10),
            (_, Some(offset)) => partial(offset as usize),
            _ => status("500 Internal Server Error"),
        })
        .await;
        let dest = dest("resume");
        let hash = client(url, 1 << 20, 0)
            .download("blob", &dest)
            .await
            .unwrap();
        assert_eq!(hash, expected());
        assert_eq!(std::fs::read(&dest).unwrap(), BLOB);
        assert_eq!(*seen.lock().unwrap(), vec![Some(0), Some(10)]);
        assert!(!partial_path(&dest).exists());
    }

    #[tokio::test]
    async fn range_ignored_restarts_from_zero() {
        let (url, seen) = server(|n, _| if n == 1 { truncated(7) } else { ok(BLOB) }).await;
        let dest = dest("norange");
        assert_eq!(
            client(url, 1 << 20, 0)
                .download("blob", &dest)
                .await
                .unwrap(),
            expected()
        );
        assert_eq!(std::fs::read(&dest).unwrap(), BLOB);
        assert_eq!(seen.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn transient_errors_retry_then_succeed() {
        let (url, seen) = server(|n, _| {
            if n < 3 {
                status("503 Service Unavailable")
            } else {
                ok(BLOB)
            }
        })
        .await;
        let dest = dest("transient");
        assert_eq!(
            client(url, 1 << 20, 0)
                .download("blob", &dest)
                .await
                .unwrap(),
            expected()
        );
        assert_eq!(seen.lock().unwrap().len(), 3);
    }

    #[tokio::test]
    async fn persistent_interruption_fails_without_a_final_file() {
        let (url, seen) = server(|_, _| truncated(5)).await;
        let dest = dest("giveup");
        let error = client(url, 1 << 20, 0)
            .download("blob", &dest)
            .await
            .unwrap_err();
        assert!(
            format!("{error:#}").contains("after 4 attempts"),
            "{error:#}"
        );
        assert!(
            !dest.exists(),
            "an incomplete artifact must never be presented"
        );
        assert_eq!(seen.lock().unwrap().len(), 4);
    }

    #[tokio::test]
    async fn expired_blob_is_permanent_and_explained() {
        let (url, seen) = server(|_, _| status("404 Not Found")).await;
        let dest = dest("expired");
        let error = client(url, 1 << 20, 0)
            .download("blob", &dest)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("unavailable or expired"), "{error}");
        assert_eq!(
            seen.lock().unwrap().len(),
            1,
            "expired blobs must not be retried"
        );
        assert!(!dest.exists());
    }

    #[tokio::test]
    async fn oversized_artifact_is_refused_before_download() {
        let (url, _) = server(|_, _| ok(BLOB)).await;
        let dest = dest("quota");
        let error = client(url, 8, 0)
            .download("blob", &dest)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("download limit"), "{error}");
        assert!(!partial_path(&dest).exists());
    }

    #[tokio::test]
    async fn insufficient_disk_space_is_refused() {
        let (url, _) = server(|_, _| ok(BLOB)).await;
        let dest = dest("disk");
        let error = client(url, 1 << 20, u64::MAX / 2)
            .download("blob", &dest)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("insufficient disk space"), "{error}");
    }

    #[tokio::test]
    async fn chunked_download_succeeds_within_reserve() {
        let (url, _) = server(|_, _| chunked(BLOB)).await;
        let dest = dest("chunked");
        let hash = client(url, 1 << 20, 0)
            .download("blob", &dest)
            .await
            .unwrap();
        assert_eq!(hash, expected());
    }

    #[tokio::test]
    async fn chunked_download_respects_the_free_space_reserve() {
        let (url, _) = server(|_, _| chunked(BLOB)).await;
        let dest = dest("chunkeddisk");
        let error = client(url, 1 << 20, u64::MAX / 2)
            .download("blob", &dest)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("insufficient disk space"), "{error}");
        assert!(!dest.exists());
        assert!(
            !partial_path(&dest).exists(),
            "refused bytes must not stay on disk"
        );
    }

    #[tokio::test]
    async fn chunked_download_respects_the_size_limit() {
        let (url, _) = server(|_, _| chunked(BLOB)).await;
        let dest = dest("chunkedquota");
        let error = client(url, 8, 0)
            .download("blob", &dest)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("download limit"), "{error}");
        assert!(!partial_path(&dest).exists());
    }

    #[tokio::test]
    async fn slow_trickle_hits_the_overall_deadline() {
        // Each byte arrives well within read_timeout, but the whole transfer
        // would take far longer than the deadline.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut request = vec![0u8; 4096];
                    let _ = socket.read(&mut request).await;
                    let head = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", BLOB.len());
                    let _ = socket.write_all(head.as_bytes()).await;
                    for byte in BLOB {
                        if socket.write_all(&[*byte]).await.is_err() {
                            return;
                        }
                        tokio::time::sleep(Duration::from_millis(200)).await;
                    }
                });
            }
        });
        let dest = dest("trickle");
        let mut client = client(url, 1 << 20, 0);
        client.deadline = Duration::from_millis(700);
        let started = std::time::Instant::now();
        let error = client
            .download("blob", &dest)
            .await
            .unwrap_err()
            .to_string();
        assert!(error.contains("did not finish within"), "{error}");
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(!dest.exists());
    }

    #[test]
    fn garbage_collection_removes_leftovers() {
        let root = dest("gc").parent().unwrap().join("artifacts");
        std::fs::create_dir_all(root.join("old")).unwrap();
        std::fs::write(root.join("old/artifact.partial"), b"stale bytes").unwrap();
        assert_eq!(collect_garbage(&root).unwrap(), 11);
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 0);
        assert_eq!(collect_garbage(&root.join("missing")).unwrap(), 0);
    }
}
