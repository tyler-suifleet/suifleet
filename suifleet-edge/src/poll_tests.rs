//! Failure-path coverage for the actual version-only gRPC gate.
use super::*;
use std::sync::{Arc, Mutex};

fn client(url: &str) -> SuiClient {
    let mut manifest: crate::config::DeploymentManifest =
        serde_json::from_str(include_str!("../../manifests/fixture.localnet.json")).unwrap();
    manifest.rpc_url = url.to_owned();
    SuiClient::load(
        SuiConfig {
            manifest_path: "fixture.json".into(),
            rpc_url: url.to_owned(),
            package_id: manifest.call_package_id.clone(),
            manifest: Some(manifest),
        },
        Arc::new(identity::Ed25519File(
            ed25519_dalek::SigningKey::from_bytes(&[7; 32]),
        )),
    )
    .unwrap()
}

#[derive(Clone)]
struct Node {
    response: Arc<Mutex<proto::BatchGetObjectsResponse>>,
    requests: Arc<Mutex<usize>>,
}
#[tonic::async_trait]
impl proto::ledger_service_server::LedgerService for Node {
    async fn batch_get_objects(
        &self,
        request: tonic::Request<proto::BatchGetObjectsRequest>,
    ) -> Result<tonic::Response<proto::BatchGetObjectsResponse>, tonic::Status> {
        let request = request.into_inner();
        assert_eq!(request.read_mask.unwrap().paths, ["version"]);
        assert_eq!(request.requests.len(), 1);
        assert_eq!(request.requests[0].read_mask.as_ref().unwrap().paths, ["version"]);
        *self.requests.lock().unwrap() += 1;
        Ok(tonic::Response::new(self.response.lock().unwrap().clone()))
    }
}

fn version(version: Option<u64>) -> proto::BatchGetObjectsResponse {
    let mut object = proto::Object::default();
    object.version = version;
    let mut result = proto::GetObjectResult::default();
    result.result = Some(proto::get_object_result::Result::Object(object));
    let mut response = proto::BatchGetObjectsResponse::default();
    response.objects = vec![result];
    response
}
fn failure(code: i32) -> proto::BatchGetObjectsResponse {
    let mut error = sui_rpc::proto::google::rpc::Status::default();
    error.code = code;
    let mut result = proto::GetObjectResult::default();
    result.result = Some(proto::get_object_result::Result::Error(error));
    let mut response = proto::BatchGetObjectsResponse::default();
    response.objects = vec![result];
    response
}

struct Server(tokio::task::JoinHandle<()>);
impl Drop for Server {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[tokio::test]
async fn unchanged_is_one_version_request_and_errors_never_authorize_idle() {
    let node = Node {
        response: Arc::new(Mutex::new(version(Some(1)))),
        requests: Default::default(),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let sui = client(&format!("http://{}", listener.local_addr().unwrap()));
    let server_node = node.clone();
    let _server = Server(tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(proto::ledger_service_server::LedgerServiceServer::new(
                server_node,
            ))
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
            .await
            .unwrap();
    }));
    let roots = vec!["0xa".to_owned()];
    sui.begin_full_poll();
    sui.observe("0xa", Some(1));
    sui.finish_full_poll();
    assert!(!sui.poll_changed(&roots, &[], 86400, 60).await.unwrap());
    assert_eq!(*node.requests.lock().unwrap(), 1);
    assert_eq!(sui.read_stats().0, 1);

    *node.response.lock().unwrap() = version(Some(2));
    assert!(sui.poll_changed(&roots, &[], 86400, 60).await.unwrap());
    *node.response.lock().unwrap() = failure(5); // NotFound triggers full validation.
    assert!(sui.poll_changed(&roots, &[], 86400, 60).await.unwrap());
    for response in [
        failure(7),
        version(None),
        proto::BatchGetObjectsResponse::default(),
    ] {
        *node.response.lock().unwrap() = response;
        assert!(sui.poll_changed(&roots, &[], 86400, 60).await.is_err());
    }
    assert_eq!(*node.requests.lock().unwrap(), 6);

    *node.response.lock().unwrap() = version(Some(1));
    {
        let mut watch = sui.poll.lock().unwrap();
        watch.full_at = Some(std::time::Instant::now() - Duration::from_secs(86401));
        watch.discovery_at = Some(std::time::Instant::now());
    }
    let generation = sui.watch_generation();
    assert!(sui.poll_changed(&roots, &[], 86400, 60).await.unwrap());
    assert_ne!(sui.watch_generation(), generation); // Wake sleeping producers too.
    {
        let mut watch = sui.poll.lock().unwrap();
        watch.full_at = Some(std::time::Instant::now());
        watch.discovery_at = Some(std::time::Instant::now() - Duration::from_secs(61));
    }
    let generation = sui.watch_generation();
    assert!(sui.poll_changed(&roots, &[], 86400, 60).await.unwrap());
    assert_eq!(sui.watch_generation(), generation); // Event scans do not wake health.
}

#[tokio::test]
async fn concurrent_reads_do_not_hide_changes_and_unused_group_fields_are_pruned() {
    let sui = client("http://127.0.0.1:1");
    let group = SuiClient::watch_id("0xa");
    let fleet = SuiClient::watch_id("0xb");
    let desired = SuiClient::watch_id("0xc");
    let writer = SuiClient::watch_id("0xd");
    sui.poll
        .lock()
        .unwrap()
        .sleeping_roots
        .insert(fleet.clone());
    sui.begin_full_poll();
    sui.watch_child(&desired, &group);
    sui.observe(&desired, Some(1));
    sui.observe(&desired, Some(2)); // Later read must not erase the earlier observation.
    sui.watch_child(&writer, &fleet);
    sui.observe(&writer, Some(3));
    sui.finish_full_poll();
    assert_eq!(sui.poll.lock().unwrap().versions[&desired], Some(1));

    sui.begin_full_poll();
    // No longer a member of group A: reconciliation does not use DesiredKey.
    sui.observe(&group, Some(4));
    sui.finish_full_poll();
    let watch = sui.poll.lock().unwrap();
    assert!(!watch.versions.contains_key(&desired));
    assert_eq!(watch.versions[&writer], Some(3)); // Sleeping fleet producer retained.
}
