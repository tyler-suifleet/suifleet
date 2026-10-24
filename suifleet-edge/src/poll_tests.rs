//! Failure-path coverage for the actual version-only gRPC gate.
use super::*;
use std::sync::{Arc, Mutex};

fn client(url: &str) -> SuiClient {
    let mut manifest: crate::config::DeploymentManifest =
        serde_json::from_str(include_str!("../../manifests/fixture.localnet.json")).unwrap();
    manifest.rpc_url = url.to_owned();
    manifest.rpc_urls = vec![url.to_owned()];
    let client = SuiClient::load(
        SuiConfig {
            manifest_path: "fixture.json".into(),
            rpc_url: url.to_owned(),
            rpc_urls: vec![url.to_owned()],
            package_id: manifest.call_package_id.clone(),
            manifest: Some(manifest),
        },
        Arc::new(identity::Ed25519File(
            ed25519_dalek::SigningKey::from_bytes(&[7; 32]),
        )),
    )
    .unwrap();
    // These gate tests model a completed deployment validation. Candidate
    // identity verification is independently exercised by rpc_tests.
    client.verified[0].store(true, std::sync::atomic::Ordering::Release);
    client
}

#[derive(Clone)]
struct Node {
    response: Arc<Mutex<proto::BatchGetObjectsResponse>>,
    requests: Arc<Mutex<usize>>,
    unavailable: Arc<Mutex<bool>>,
    wrong_chain: bool,
}
#[tonic::async_trait]
impl proto::ledger_service_server::LedgerService for Node {
    async fn get_service_info(&self, _: tonic::Request<proto::GetServiceInfoRequest>) -> Result<tonic::Response<proto::GetServiceInfoResponse>, tonic::Status> {
        let mut genesis = [0; 32];
        genesis[..4].copy_from_slice(&[0x12, 0x34, 0xab, 0xcd]);
        if self.wrong_chain { genesis[0] = 0xff; }
        let mut info = proto::GetServiceInfoResponse::default();
        info.chain_id = Some(sui_sdk_types::Digest::new(genesis).to_string());
        Ok(tonic::Response::new(info))
    }
    async fn batch_get_objects(
        &self,
        request: tonic::Request<proto::BatchGetObjectsRequest>,
    ) -> Result<tonic::Response<proto::BatchGetObjectsResponse>, tonic::Status> {
        let request = request.into_inner();
        assert_eq!(request.read_mask.unwrap().paths, ["version"]);
        assert_eq!(request.requests.len(), 1);
        assert_eq!(
            request.requests[0].read_mask.as_ref().unwrap().paths,
            ["version"]
        );
        *self.requests.lock().unwrap() += 1;
        if *self.unavailable.lock().unwrap() {
            return Err(tonic::Status::unavailable("fixture outage"));
        }
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
        unavailable: Default::default(),
        wrong_chain: false,
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
    *node.unavailable.lock().unwrap() = true;
    assert!(sui.poll_changed(&roots, &[], 86400, 60).await.is_err());
    assert_eq!(sui.read_stats().0, 7); // Failed transport attempt is counted too.
    *node.unavailable.lock().unwrap() = false;

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

#[derive(Clone)]
struct HistoryNode {
    floor: Arc<std::sync::atomic::AtomicU64>,
    starts: Arc<Mutex<Vec<u64>>>,
    package_reads: Arc<Mutex<u64>>,
    publication_reads: Arc<Mutex<u64>>,
}
#[tonic::async_trait]
impl proto::ledger_service_server::LedgerService for HistoryNode {
    async fn get_service_info(
        &self,
        _: tonic::Request<proto::GetServiceInfoRequest>,
    ) -> Result<tonic::Response<proto::GetServiceInfoResponse>, tonic::Status> {
        let mut info = proto::GetServiceInfoResponse::default();
        let mut genesis = [0; 32];
        genesis[..4].copy_from_slice(&[0x12, 0x34, 0xab, 0xcd]);
        info.chain_id = Some(sui_sdk_types::Digest::new(genesis).to_string());
        info.lowest_available_checkpoint =
            Some(self.floor.load(std::sync::atomic::Ordering::SeqCst));
        Ok(tonic::Response::new(info))
    }
    async fn get_object(
        &self,
        request: tonic::Request<proto::GetObjectRequest>,
    ) -> Result<tonic::Response<proto::GetObjectResponse>, tonic::Status> {
        *self.package_reads.lock().unwrap() += 1;
        let package = sui_sdk_types::MovePackage {
            id: Address::from_str(request.into_inner().object_id.as_ref().unwrap()).unwrap(),
            version: 1,
            modules: Default::default(),
            type_origin_table: vec![],
            linkage_table: Default::default(),
        };
        let object = sui_sdk_types::Object::new(
            sui_sdk_types::ObjectData::Package(package),
            sui_sdk_types::Owner::Immutable,
            sui_sdk_types::Digest::new([7; 32]),
            0,
        );
        let mut bytes = proto::Bcs::default();
        bytes.value = Some(bcs::to_bytes(&object).unwrap().into());
        let mut object = proto::Object::default();
        object.bcs = Some(bytes);
        let mut response = proto::GetObjectResponse::default();
        response.object = Some(object);
        Ok(tonic::Response::new(response))
    }
    async fn get_transaction(
        &self,
        request: tonic::Request<proto::GetTransactionRequest>,
    ) -> Result<tonic::Response<proto::GetTransactionResponse>, tonic::Status> {
        assert_eq!(
            request.into_inner().digest.unwrap(),
            sui_sdk_types::Digest::new([7; 32]).to_string()
        );
        *self.publication_reads.lock().unwrap() += 1;
        let mut transaction = proto::ExecutedTransaction::default();
        transaction.checkpoint = Some(10);
        let mut response = proto::GetTransactionResponse::default();
        response.transaction = Some(transaction);
        Ok(tonic::Response::new(response))
    }
    async fn list_events(
        &self,
        request: tonic::Request<proto::ListEventsRequest>,
    ) -> Result<tonic::Response<tonic::codegen::BoxStream<proto::ListEventsResponse>>, tonic::Status>
    {
        self.starts
            .lock()
            .unwrap()
            .push(request.into_inner().start_checkpoint.unwrap());
        let mut watermark = proto::Watermark::default();
        watermark.cursor = Some(vec![1].into());
        watermark.checkpoint = Some(20);
        let mut end = proto::QueryEnd::default();
        end.reason = Some(proto::QueryEndReason::LedgerTip as i32);
        let mut frame = proto::ListEventsResponse::default();
        frame.watermark = Some(watermark);
        frame.end = Some(end);
        Ok(tonic::Response::new(Box::pin(tokio_stream::iter([Ok(
            frame,
        )]))))
    }
}

#[tokio::test]
async fn missing_membership_cursor_uses_verified_type_birth_without_skipping_pruned_history() {
    let node = HistoryNode {
        floor: Arc::new(std::sync::atomic::AtomicU64::new(5)),
        starts: Default::default(),
        package_reads: Default::default(),
        publication_reads: Default::default(),
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
    sui.memberships(&None).await.unwrap();
    sui.memberships(&None).await.unwrap();
    assert_eq!(*node.starts.lock().unwrap(), [10, 10]); // Inclusive type publication.
    assert_eq!(*node.package_reads.lock().unwrap(), 1);
    assert_eq!(*node.publication_reads.lock().unwrap(), 1); // Manifest-bound immutable cache.
    assert!(sui
        .memberships(&Some(json!({"checkpoint": 4})))
        .await
        .err()
        .unwrap()
        .to_string()
        .contains("pruned"));
    node.floor.store(11, std::sync::atomic::Ordering::SeqCst);
    assert!(sui
        .memberships(&None)
        .await
        .err()
        .unwrap()
        .to_string()
        .contains("pruned"));
    assert_eq!(node.starts.lock().unwrap().len(), 2); // No query may silently jump over missing membership history.
}

async fn serve_watch(node: Node) -> (String, Server) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = Server(tokio::spawn(async move {
        tonic::transport::Server::builder().add_service(proto::ledger_service_server::LedgerServiceServer::new(node))
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener)).await.unwrap();
    }));
    (url, server)
}

#[tokio::test]
async fn version_batch_fallback_counts_attempts_and_verifies_chain_before_reading() {
    let primary = Node { response: Arc::new(Mutex::new(version(Some(1)))), requests: Default::default(), unavailable: Arc::new(Mutex::new(true)), wrong_chain: false };
    let backup = Node { response: Arc::new(Mutex::new(version(Some(1)))), requests: Default::default(), unavailable: Default::default(), wrong_chain: false };
    let (primary_url, _primary) = serve_watch(primary.clone()).await;
    let (backup_url, _backup) = serve_watch(backup.clone()).await;
    let base = client(&primary_url);
    let mut config = base.config.clone(); config.rpc_urls.push(backup_url);
    let sui = SuiClient::load(config.clone(), base.signer.clone()).unwrap();
    sui.verified[0].store(true, std::sync::atomic::Ordering::Release);
    sui.begin_full_poll(); sui.observe("0xa", Some(1)); sui.finish_full_poll();
    assert!(!sui.poll_changed(&["0xa".into()], &[], 86400, 60).await.unwrap());
    assert_eq!(*primary.requests.lock().unwrap(), 1);
    assert_eq!(*backup.requests.lock().unwrap(), 1);
    assert_eq!(sui.read_stats().0, 3); // Failed batch + identity probe + successful batch.
    *primary.unavailable.lock().unwrap() = false;
    assert!(!sui.poll_changed(&["0xa".into()], &[], 86400, 60).await.unwrap());
    assert_eq!(sui.read_stats().0, 4); // Healthy primary remains one read.

    let wrong = Node { wrong_chain: true, ..backup.clone() };
    *wrong.requests.lock().unwrap() = 0;
    let (wrong_url, _wrong) = serve_watch(wrong.clone()).await;
    config.rpc_urls[1] = wrong_url;
    let sui = SuiClient::load(config, base.signer.clone()).unwrap();
    sui.verified[0].store(true, std::sync::atomic::Ordering::Release);
    *primary.unavailable.lock().unwrap() = true;
    sui.begin_full_poll(); sui.observe("0xa", Some(1)); sui.finish_full_poll();
    let error = sui.poll_changed(&["0xa".into()], &[], 86400, 60).await.err().unwrap();
    assert!(error.to_string().contains("chain identifier mismatch"), "{error}");
    assert_eq!(*wrong.requests.lock().unwrap(), 0); // No unverified object read.
    assert_eq!(sui.read_stats().0, 2); // Failed primary + rejected identity probe.
}
