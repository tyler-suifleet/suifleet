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
            http: reqwest::Client::new(),
        }
    }

    /// Download a blob and write it to `dest`. Returns the sha256 hex of the downloaded bytes.
    pub async fn download(&self, blob_id: &str, dest: &Path) -> anyhow::Result<String> {
        let url = format!("{}/v1/blobs/{}", self.aggregator_url, blob_id);
        info!("Downloading blob {} from {}", blob_id, url);

        let res = self.http.get(&url).send().await?.error_for_status()?;
        let bytes = res.bytes().await?;

        let hash = hex::encode(Sha256::digest(&bytes));
        std::fs::write(dest, &bytes)?;

        info!("Downloaded {} bytes, sha256={}", bytes.len(), hash);
        Ok(hash)
    }
}
