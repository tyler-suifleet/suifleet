//! Test-only gRPC proxy that makes a real (local) Sui node look like a
//! public node that has pruned its event history (SF-125).
//!
//! Every RPC the daemon uses is forwarded unchanged to the upstream node,
//! except that, while the file LOWEST_FILE holds a checkpoint above 0:
//!
//! - GetServiceInfo reports that checkpoint as lowest_available_checkpoint;
//! - ListEvents fails with OUT_OF_RANGE, as Mysten's public nodes do for a
//!   range below their retained history, unless it starts at or above it.
//!
//! Usage: grpc_prune_proxy UPSTREAM_URL LOWEST_FILE  (prints "listening PORT")
use std::{path::PathBuf, sync::Arc};

use sui_rpc::proto::sui::rpc::v2 as proto;
use tonic::{Request, Response, Status};

#[derive(Clone)]
struct Proxy {
    upstream: sui_rpc::Client,
    lowest: Arc<PathBuf>,
}

impl Proxy {
    fn lowest(&self) -> u64 {
        std::fs::read_to_string(&*self.lowest)
            .ok()
            .and_then(|text| text.trim().parse().ok())
            .unwrap_or(0)
    }
}

#[tonic::async_trait]
impl proto::ledger_service_server::LedgerService for Proxy {
    async fn get_service_info(
        &self,
        request: Request<proto::GetServiceInfoRequest>,
    ) -> Result<Response<proto::GetServiceInfoResponse>, Status> {
        let mut response = self.upstream.clone().ledger_client().get_service_info(request.into_inner()).await?;
        let lowest = self.lowest();
        if lowest > 0 {
            response.get_mut().lowest_available_checkpoint = Some(lowest);
        }
        Ok(response)
    }

    async fn get_object(
        &self,
        request: Request<proto::GetObjectRequest>,
    ) -> Result<Response<proto::GetObjectResponse>, Status> {
        self.upstream.clone().ledger_client().get_object(request.into_inner()).await
    }

    async fn batch_get_objects(
        &self,
        request: Request<proto::BatchGetObjectsRequest>,
    ) -> Result<Response<proto::BatchGetObjectsResponse>, Status> {
        self.upstream.clone().ledger_client().batch_get_objects(request.into_inner()).await
    }

    async fn get_transaction(
        &self,
        request: Request<proto::GetTransactionRequest>,
    ) -> Result<Response<proto::GetTransactionResponse>, Status> {
        self.upstream.clone().ledger_client().get_transaction(request.into_inner()).await
    }

    async fn get_checkpoint(
        &self,
        request: Request<proto::GetCheckpointRequest>,
    ) -> Result<Response<proto::GetCheckpointResponse>, Status> {
        self.upstream.clone().ledger_client().get_checkpoint(request.into_inner()).await
    }

    async fn list_events(
        &self,
        request: Request<proto::ListEventsRequest>,
    ) -> Result<Response<tonic::codegen::BoxStream<proto::ListEventsResponse>>, Status> {
        let request = request.into_inner();
        let lowest = self.lowest();
        // A resume cursor is opaque: treat any cursor as below the history,
        // like a range that starts before the lowest retained checkpoint.
        let resumes = request.options.as_ref().and_then(|o| o.after.as_ref()).is_some();
        if lowest > 0 && (resumes || request.start_checkpoint.unwrap_or(0) < lowest) {
            return Err(Status::out_of_range("requested range is below the lowest available checkpoint"));
        }
        let stream = self.upstream.clone().ledger_client().list_events(request).await?.into_inner();
        Ok(Response::new(Box::pin(stream)))
    }
}

#[tonic::async_trait]
impl proto::state_service_server::StateService for Proxy {
    async fn get_balance(
        &self,
        request: Request<proto::GetBalanceRequest>,
    ) -> Result<Response<proto::GetBalanceResponse>, Status> {
        self.upstream.clone().state_client().get_balance(request.into_inner()).await
    }

    async fn list_owned_objects(
        &self,
        request: Request<proto::ListOwnedObjectsRequest>,
    ) -> Result<Response<proto::ListOwnedObjectsResponse>, Status> {
        self.upstream.clone().state_client().list_owned_objects(request.into_inner()).await
    }

    async fn list_dynamic_fields(
        &self,
        request: Request<proto::ListDynamicFieldsRequest>,
    ) -> Result<Response<proto::ListDynamicFieldsResponse>, Status> {
        self.upstream.clone().state_client().list_dynamic_fields(request.into_inner()).await
    }
}

#[tonic::async_trait]
impl proto::transaction_execution_service_server::TransactionExecutionService for Proxy {
    async fn simulate_transaction(
        &self,
        request: Request<proto::SimulateTransactionRequest>,
    ) -> Result<Response<proto::SimulateTransactionResponse>, Status> {
        self.upstream.clone().execution_client().simulate_transaction(request.into_inner()).await
    }

    async fn execute_transaction(
        &self,
        request: Request<proto::ExecuteTransactionRequest>,
    ) -> Result<Response<proto::ExecuteTransactionResponse>, Status> {
        self.upstream.clone().execution_client().execute_transaction(request.into_inner()).await
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    let (Some(upstream), Some(lowest)) = (args.next(), args.next()) else {
        anyhow::bail!("usage: grpc_prune_proxy UPSTREAM_URL LOWEST_FILE");
    };
    let proxy = Proxy {
        upstream: sui_rpc::Client::new(upstream.as_str()).map_err(|e| anyhow::anyhow!("invalid upstream: {e}"))?,
        lowest: Arc::new(PathBuf::from(lowest)),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    println!("listening {}", listener.local_addr()?.port());
    use std::io::Write;
    std::io::stdout().flush()?;
    tonic::transport::Server::builder()
        .add_service(proto::ledger_service_server::LedgerServiceServer::new(proxy.clone()))
        .add_service(proto::state_service_server::StateServiceServer::new(proxy.clone()))
        .add_service(proto::transaction_execution_service_server::TransactionExecutionServiceServer::new(proxy))
        .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
        .await?;
    Ok(())
}
