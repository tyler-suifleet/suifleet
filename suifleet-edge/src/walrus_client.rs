use std::path::Path;

use sha2::{Digest, Sha256};
use tracing::info;

pub struct WalrusClient {
    aggregator_url: String,
    http: reqwest::Client,
}

impl WalrusClient {
    pub fn new(aggregator_url: String) -> Self {
        Self {
            aggregator_url,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(300))
                .build()
                .expect("HTTP client"),
        }
    }

    pub async fn recipe(&self, blob_id: &str) -> anyhow::Result<(crate::recipe::Recipe, String)> {
        use futures_util::StreamExt;
        let url = format!(
            "{}/v1/blobs/{}",
            self.aggregator_url.trim_end_matches('/'),
            blob_id
        );
        let response = self.http.get(url).send().await?.error_for_status()?;
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            anyhow::ensure!(
                bytes.len() + chunk.len() <= 1024 * 1024,
                "recipe exceeds 1 MiB limit"
            );
            bytes.extend_from_slice(&chunk);
        }
        Ok((
            serde_json::from_slice(&bytes)?,
            hex::encode(Sha256::digest(&bytes)),
        ))
    }

    /// Download a blob and write it to `dest`. Returns the sha256 hex of the downloaded bytes.
    pub async fn download(&self, blob_id: &str, dest: &Path) -> anyhow::Result<String> {
        let url = format!("{}/v1/blobs/{}", self.aggregator_url, blob_id);
        info!("Downloading blob {} from {}", blob_id, url);

        let res = self.http.get(&url).send().await?.error_for_status()?;
        use futures_util::StreamExt;
        use std::io::Write;
        let mut file = std::fs::File::create(dest)?;
        let mut stream = res.bytes_stream();
        let mut hasher = Sha256::new();
        let mut length = 0u64;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            length += chunk.len() as u64;
            anyhow::ensure!(
                length <= 1024 * 1024 * 1024,
                "artifact exceeds 1 GiB download limit"
            );
            hasher.update(&chunk);
            file.write_all(&chunk)?;
        }
        file.sync_all()?;

        let hash = hex::encode(hasher.finalize());

        info!("Downloaded {} bytes, sha256={}", length, hash);
        Ok(hash)
    }
}
