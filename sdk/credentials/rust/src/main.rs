//! Unmodified AWS SDK credential chain. Never print credential material.
use aws_credential_types::provider::ProvideCredentials;
#[tokio::main]
async fn main() {
    let config = aws_config::defaults(aws_config::BehaviorVersion::latest()).load().await;
    let provider = config.credentials_provider().expect("default SDK credential chain");
    let credentials = provider.provide_credentials().await.expect("container credentials");
    assert_eq!(credentials.access_key_id(), "AKIAFIXTURE");
    assert_eq!(credentials.secret_access_key(), "fixture-secret");
    assert_eq!(credentials.session_token(), Some("fixture-session"));
    println!("PASS: AWS SDK for Rust default credential chain");
}
