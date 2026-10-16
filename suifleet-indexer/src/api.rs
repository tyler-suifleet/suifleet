//! Versioned HTTP API (`/v1`) over the materialized fleet state, plus
//! unsigned bulk-operation transactions. The service never holds keys.
//!
//! Auth: bearer tokens from a file of `read|operate TOKEN` lines, compared in
//! constant time. `operate` covers `read`. With `public_read`, GET requests
//! need no token (for local use only). Device telemetry ingress
//! (`POST /v1/telemetry`, SF-019) takes no token: each snapshot carries the
//! device's own signature, checked against its registry identity.
use crate::chain::Chain;
use crate::store::{self, Filter, Readers};
use crate::txbuild;
use axum::{
    extract::{DefaultBodyLimit, Path, Query, Request, State},
    http::{header, HeaderValue, Method, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Scope {
    Read,
    Operate,
}

#[derive(Clone)]
pub struct Tokens(Arc<Vec<(Scope, Vec<u8>)>>);

impl Tokens {
    pub fn parse(text: &str) -> anyhow::Result<Self> {
        let mut tokens = Vec::new();
        for (number, line) in text.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (scope, token) = line.split_once(char::is_whitespace).ok_or_else(|| {
                anyhow::anyhow!(
                    "token file line {}: expected `read|operate TOKEN`",
                    number + 1
                )
            })?;
            let scope = match scope {
                "read" => Scope::Read,
                "operate" => Scope::Operate,
                other => anyhow::bail!("token file line {}: unknown scope {other}", number + 1),
            };
            let token = token.trim();
            anyhow::ensure!(
                token.len() >= 32,
                "token file line {}: tokens need at least 32 characters",
                number + 1
            );
            tokens.push((scope, token.as_bytes().to_vec()));
        }
        Ok(Self(Arc::new(tokens)))
    }

    /// The scope of `presented`, checking every token in constant time.
    fn scope(&self, presented: &[u8]) -> Option<Scope> {
        let mut found = None;
        for (scope, token) in self.0.iter() {
            // Full-width: any length mismatch must survive (a u8 truncation
            // would accept the token followed by 256 extra bytes).
            let mut difference = token.len() ^ presented.len();
            for (index, byte) in token.iter().enumerate() {
                difference |= usize::from(byte ^ presented.get(index).copied().unwrap_or(0));
            }
            if difference == 0 {
                found = found.max(Some(*scope));
            }
        }
        found
    }
}

#[derive(Clone)]
pub struct AppState {
    pub readers: Readers,
    pub tokens: Tokens,
    pub public_read: bool,
    pub chain: Chain,
    pub call_package: String,
    pub registry: String,
    pub types: Arc<std::collections::BTreeMap<String, String>>,
    /// Telemetry ingress and health (`--telemetry-db`); disabled when none.
    pub telemetry: Option<Arc<crate::telemetry::Service>>,
    /// Browser origin (the dApp) allowed to call the API (`--cors-origin`).
    pub cors_origin: Option<String>,
}

pub struct ApiError(StatusCode, String);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({ "error": self.1 }))).into_response()
    }
}

impl From<anyhow::Error> for ApiError {
    fn from(error: anyhow::Error) -> Self {
        match error.downcast_ref::<txbuild::Invalid>() {
            Some(invalid) => ApiError(StatusCode::BAD_REQUEST, invalid.0.clone()),
            None => ApiError(StatusCode::INTERNAL_SERVER_ERROR, format!("{error:#}")),
        }
    }
}

type ApiResult<T> = Result<T, ApiError>;

async fn auth(State(state): State<AppState>, request: Request, next: Next) -> Response {
    let needed = if request.method() == Method::GET {
        Scope::Read
    } else {
        Scope::Operate
    };
    let presented = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .map(|token| token.as_bytes().to_vec());
    let granted = presented
        .as_deref()
        .and_then(|token| state.tokens.scope(token));
    let allowed = match granted {
        Some(scope) => scope >= needed,
        None => presented.is_none() && needed == Scope::Read && state.public_read,
    };
    if allowed {
        return next.run(request).await;
    }
    let status = if presented.is_none() || granted.is_none() {
        StatusCode::UNAUTHORIZED
    } else {
        StatusCode::FORBIDDEN
    };
    ApiError(
        status,
        "missing, unknown or insufficient bearer token".into(),
    )
    .into_response()
}

async fn blocking<T: Send + 'static>(
    readers: &Readers,
    work: impl FnOnce(&rusqlite::Connection) -> anyhow::Result<T> + Send + 'static,
) -> ApiResult<T> {
    let readers = readers.clone();
    tokio::task::spawn_blocking(move || work(&readers.open()?))
        .await
        .map_err(|e| ApiError(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?
        .map_err(ApiError::from)
}

#[derive(Debug, Deserialize, Default)]
pub struct ListQuery {
    owner: Option<String>,
    admin: Option<String>,
    creator: Option<String>,
    group: Option<String>,
    device: Option<String>,
    component: Option<String>,
    cursor: Option<String>,
    limit: Option<usize>,
    #[serde(default)]
    include_deleted: bool,
}

fn id_param(value: &Option<String>, name: &str) -> ApiResult<Option<String>> {
    value
        .as_ref()
        .map(|v| {
            crate::chain::normalize(v)
                .ok_or_else(|| ApiError(StatusCode::BAD_REQUEST, format!("invalid {name} ID")))
        })
        .transpose()
}

impl ListQuery {
    fn filter(&self, owner: &Option<String>) -> ApiResult<Filter> {
        Ok(Filter {
            owner: id_param(owner, "owner")?,
            group: id_param(&self.group, "group")?,
            device: id_param(&self.device, "device")?,
            component: self.component.clone(),
            include_deleted: self.include_deleted,
        })
    }
    fn cursor(&self) -> ApiResult<Option<String>> {
        id_param(&self.cursor, "cursor")
    }
}

async fn status(State(state): State<AppState>) -> ApiResult<Json<Value>> {
    let tip = state.chain.service_info().await.ok();
    let (rows, chain_identifier, last_change, reconciled) = blocking(&state.readers, |db| {
        Ok((
            store::cursors(db)?,
            store::meta(db, "chain_identifier")?,
            store::last_change(db)?,
            store::meta(db, crate::indexer::SWEEP_COMPLETED)?,
        ))
    })
    .await?;
    let checkpoint_height = tip.as_ref().and_then(|t| t.checkpoint_height);
    Ok(Json(json!({
        "api_version": 1,
        "chain_identifier": chain_identifier,
        "call_package_id": state.call_package,
        "checkpoint_height": checkpoint_height,
        "lowest_available_checkpoint": tip.as_ref().and_then(|t| t.lowest_available_checkpoint),
        "last_change": last_change,
        "devices_reconciled_at": reconciled.and_then(|t| t.parse::<u64>().ok()),
        "streams": rows.iter().map(|(stream, checkpoint, updated_at)| json!({
            "stream": stream, "checkpoint": checkpoint, "updated_at": updated_at,
            "lag_checkpoints": checkpoint.zip(checkpoint_height).map(|(c, h)| h.saturating_sub(c)),
        })).collect::<Vec<_>>(),
    })))
}

async fn devices(
    State(state): State<AppState>,
    Query(query): Query<ListQuery>,
) -> ApiResult<Json<Value>> {
    let filter = query.filter(&query.owner)?;
    let (cursor, limit) = (query.cursor()?, query.limit);
    let page = blocking(&state.readers, move |db| {
        store::list_devices(db, &filter, cursor.as_deref(), limit)
    })
    .await?;
    Ok(Json(
        serde_json::to_value(page).map_err(anyhow::Error::from)?,
    ))
}

async fn groups(
    State(state): State<AppState>,
    Query(query): Query<ListQuery>,
) -> ApiResult<Json<Value>> {
    let filter = query.filter(&query.admin)?;
    let (cursor, limit) = (query.cursor()?, query.limit);
    let page = blocking(&state.readers, move |db| {
        store::list_groups(db, &filter, cursor.as_deref(), limit)
    })
    .await?;
    Ok(Json(
        serde_json::to_value(page).map_err(anyhow::Error::from)?,
    ))
}

async fn deployments(
    State(state): State<AppState>,
    Query(query): Query<ListQuery>,
) -> ApiResult<Json<Value>> {
    let filter = query.filter(&query.creator)?;
    let (cursor, limit) = (query.cursor()?, query.limit);
    let page = blocking(&state.readers, move |db| {
        store::list_deployments(db, &filter, cursor.as_deref(), limit)
    })
    .await?;
    Ok(Json(
        serde_json::to_value(page).map_err(anyhow::Error::from)?,
    ))
}

fn not_found(what: &str) -> ApiError {
    ApiError(StatusCode::NOT_FOUND, format!("{what} not indexed"))
}

async fn device(State(state): State<AppState>, Path(id): Path<String>) -> ApiResult<Json<Value>> {
    let id = id_param(&Some(id), "device")?.unwrap();
    let found = blocking(&state.readers, move |db| store::read_device(db, &id)).await?;
    Ok(Json(
        serde_json::to_value(found.ok_or_else(|| not_found("device"))?)
            .map_err(anyhow::Error::from)?,
    ))
}

async fn group(State(state): State<AppState>, Path(id): Path<String>) -> ApiResult<Json<Value>> {
    let id = id_param(&Some(id), "group")?.unwrap();
    let found = blocking(&state.readers, move |db| store::read_group(db, &id)).await?;
    Ok(Json(
        serde_json::to_value(found.ok_or_else(|| not_found("group"))?)
            .map_err(anyhow::Error::from)?,
    ))
}

async fn deployment(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> ApiResult<Json<Value>> {
    let id = id_param(&Some(id), "deployment")?.unwrap();
    let found = blocking(&state.readers, move |db| {
        let Some(deployment) = store::read_deployment(db, &id)? else {
            return Ok(None);
        };
        let resolved = store::resolved_devices(db, &deployment)?;
        let receipts = store::read_receipts(db, &id)?;
        Ok(Some((deployment, resolved, receipts)))
    })
    .await?;
    let (deployment, resolved, receipts) = found.ok_or_else(|| not_found("deployment"))?;
    let mut body = serde_json::to_value(deployment).map_err(anyhow::Error::from)?;
    body["resolved_devices"] = json!(resolved);
    body["receipts"] = json!(receipts.iter().map(|(device, status)| json!({
        "device_id": device, "status": status, "label": if *status == 1 { "applied" } else { "failed" },
    })).collect::<Vec<_>>());
    Ok(Json(body))
}

#[derive(Debug, Deserialize)]
pub struct ChangesQuery {
    #[serde(default)]
    after: i64,
    limit: Option<usize>,
    /// Long-poll: wait up to this many seconds (max 30) for a change.
    #[serde(default)]
    wait: u64,
}

async fn changes(
    State(state): State<AppState>,
    Query(query): Query<ChangesQuery>,
) -> ApiResult<Json<Value>> {
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(query.wait.min(30));
    loop {
        let (after, limit) = (query.after, query.limit);
        let items = blocking(&state.readers, move |db| {
            store::changes_after(db, after, limit)
        })
        .await?;
        if !items.is_empty() || tokio::time::Instant::now() >= deadline {
            let next = items.last().map(|c| c.seq).unwrap_or(query.after);
            return Ok(Json(json!({ "items": items, "next": next })));
        }
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    }
}

async fn transaction(
    State(state): State<AppState>,
    Path(operation): Path<String>,
    Json(request): Json<txbuild::Request>,
) -> ApiResult<Json<Value>> {
    // Ownership and identity prechecks use the devices as they are on chain
    // now, not the index: a transfer of a Device or its owner cap emits no
    // fleet event and waits for the reconciliation sweep.
    let ids = txbuild::devices_to_check(&operation, &request);
    let read = crate::devices::read(&state.chain, &state.types, &state.registry, &ids).await?;
    let live: std::collections::BTreeMap<_, _> = ids.into_iter().zip(read).collect();
    let readers = state.readers.clone();
    let package = state.call_package.clone();
    let registry = state.registry.clone();
    let operation_name = operation.clone();
    let planned = blocking(&readers, move |db| {
        txbuild::plan(db, &package, &registry, &operation_name, &request, &live)
    })
    .await?;
    let built = txbuild::build(&state.chain, planned).await?;
    Ok(Json(built))
}

/// Answer CORS preflights and mark responses for the one configured origin;
/// other origins get no CORS headers, so browsers keep blocking them.
async fn cors(State(state): State<AppState>, request: Request, next: Next) -> Response {
    let allowed = state.cors_origin.as_deref().filter(|origin| {
        request.headers().get(header::ORIGIN).and_then(|o| o.to_str().ok()) == Some(*origin)
    });
    let Some(origin) = allowed.and_then(|o| HeaderValue::from_str(o).ok()) else {
        return next.run(request).await;
    };
    let mut response = if request.method() == Method::OPTIONS {
        let mut response = StatusCode::NO_CONTENT.into_response();
        let headers = response.headers_mut();
        headers.insert(header::ACCESS_CONTROL_ALLOW_METHODS, HeaderValue::from_static("GET, POST"));
        headers.insert(header::ACCESS_CONTROL_ALLOW_HEADERS, HeaderValue::from_static("authorization, content-type"));
        headers.insert(header::ACCESS_CONTROL_MAX_AGE, HeaderValue::from_static("600"));
        response
    } else {
        next.run(request).await
    };
    response.headers_mut().insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin);
    response.headers_mut().insert(header::VARY, HeaderValue::from_static("origin"));
    response
}

fn telemetry_service(state: &AppState) -> ApiResult<Arc<crate::telemetry::Service>> {
    state.telemetry.clone().ok_or_else(|| {
        ApiError(StatusCode::NOT_FOUND, "telemetry is not enabled on this indexer (--telemetry-db)".into())
    })
}

#[derive(Debug, Deserialize)]
pub struct Ingress {
    messages: Vec<crate::telemetry::Envelope>,
}

/// Signed device snapshots; one verdict per envelope, in order.
async fn telemetry_ingest(State(state): State<AppState>, Json(body): Json<Ingress>) -> ApiResult<Json<Value>> {
    let service = telemetry_service(&state)?;
    if body.messages.len() > crate::telemetry::MAX_BATCH {
        return Err(ApiError(
            StatusCode::BAD_REQUEST,
            format!("at most {} snapshots per request", crate::telemetry::MAX_BATCH),
        ));
    }
    let results = blocking(&state.readers, move |index| {
        let writer = service.writer.lock().unwrap();
        let now = crate::telemetry::now_ms();
        body.messages
            .iter()
            .map(|envelope| {
                crate::telemetry::ingest(&writer, &service.binding, service.retain,
                    |id| store::read_device(index, id), envelope, now)
            })
            .collect::<anyhow::Result<Vec<_>>>()
    })
    .await?;
    Ok(Json(json!({ "results": results })))
}

/// Status, last seen time and inventory of each device the filter selects.
async fn health(State(state): State<AppState>, Query(query): Query<ListQuery>) -> ApiResult<Json<Value>> {
    let service = telemetry_service(&state)?;
    let filter = query.filter(&query.owner)?;
    let (cursor, limit) = (query.cursor()?, query.limit);
    let body = blocking(&state.readers, move |db| {
        let page = store::list_devices(db, &filter, cursor.as_deref(), limit)?;
        let telemetry = service.reader()?;
        let now = crate::telemetry::now_ms();
        let items = page
            .items
            .iter()
            .map(|device| crate::telemetry::device_health(&telemetry, device, now))
            .collect::<anyhow::Result<Vec<_>>>()?;
        Ok(json!({ "items": items, "next": page.next, "server_time_ms": now }))
    })
    .await?;
    Ok(Json(body))
}

/// Fleet aggregate: how many selected devices are in each status.
async fn health_summary(State(state): State<AppState>, Query(query): Query<ListQuery>) -> ApiResult<Json<Value>> {
    let service = telemetry_service(&state)?;
    let filter = query.filter(&query.owner)?;
    let body = blocking(&state.readers, move |db| {
        let telemetry = service.reader()?;
        let now = crate::telemetry::now_ms();
        let mut counts = std::collections::BTreeMap::<String, u64>::new();
        for status in ["healthy", "degraded", "offline", "unreported"] {
            counts.insert(status.into(), 0);
        }
        let mut cursor = None;
        loop {
            let page = store::list_devices(db, &filter, cursor.as_deref(), Some(store::MAX_LIMIT))?;
            for device in &page.items {
                let status = crate::telemetry::device_health(&telemetry, device, now)?.assessment.status;
                let name = serde_json::to_value(status)?.as_str().unwrap_or_default().to_owned();
                *counts.entry(name).or_default() += 1;
            }
            match page.next {
                Some(next) => cursor = Some(next),
                None => break,
            }
        }
        let total: u64 = counts.values().sum();
        Ok(json!({ "total": total, "counts": counts, "server_time_ms": now }))
    })
    .await?;
    Ok(Json(body))
}

#[derive(Debug, Deserialize)]
pub struct HistoryQuery {
    before: Option<u64>,
    limit: Option<usize>,
}

/// A device's stored snapshots (signed, as received), newest first.
async fn telemetry_history(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Query(query): Query<HistoryQuery>,
) -> ApiResult<Json<Value>> {
    let service = telemetry_service(&state)?;
    let id = id_param(&Some(id), "device")?.unwrap();
    let limit = query.limit.unwrap_or(50).clamp(1, store::MAX_LIMIT);
    let items = blocking(&state.readers, move |_| {
        crate::telemetry::history(&service.reader()?, &id, query.before, limit)
    })
    .await?;
    let next = (items.len() == limit).then(|| items.last().map(|r| r.seq)).flatten();
    Ok(Json(json!({ "items": items, "next": next })))
}

pub fn router(state: AppState) -> Router {
    // Signature-authenticated, so outside the bearer-token layer.
    let ingress = Router::new()
        .route("/v1/telemetry", post(telemetry_ingest))
        .layer(DefaultBodyLimit::max(2 * 1024 * 1024));
    Router::new()
        .route("/v1/status", get(status))
        .route("/v1/devices", get(devices))
        .route("/v1/devices/{id}", get(device))
        .route("/v1/groups", get(groups))
        .route("/v1/groups/{id}", get(group))
        .route("/v1/deployments", get(deployments))
        .route("/v1/deployments/{id}", get(deployment))
        .route("/v1/changes", get(changes))
        .route("/v1/transactions/{operation}", post(transaction))
        .route("/v1/health", get(health))
        .route("/v1/health/summary", get(health_summary))
        .route("/v1/devices/{id}/telemetry", get(telemetry_history))
        .layer(middleware::from_fn_with_state(state.clone(), auth))
        .merge(ingress)
        .layer(middleware::from_fn_with_state(state.clone(), cors))
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::telemetry::tests as fixture;
    use tower::ServiceExt;

    const READ: &str = "rrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrr";

    fn app(dir: &std::path::Path, telemetry: bool) -> Router {
        let index = dir.join("fleet.db");
        let mut db = store::open(&index).unwrap();
        let tx = db.transaction().unwrap();
        store::put_device(&tx, &fixture::device(&fixture::address(&fixture::KEY)), None).unwrap();
        tx.commit().unwrap();
        let service = telemetry.then(|| {
            let path = dir.join("telemetry.db");
            let writer = crate::telemetry::open(&path, &fixture::binding(), false).unwrap();
            Arc::new(crate::telemetry::Service { path, binding: fixture::binding(), retain: 10, writer: std::sync::Mutex::new(writer) })
        });
        router(AppState {
            readers: Readers::new(&index),
            tokens: Tokens::parse(&format!("read {READ}\n")).unwrap(),
            public_read: false,
            chain: Chain::new("http://127.0.0.1:9").unwrap(),
            call_package: String::new(),
            registry: fixture::binding().registry,
            types: Arc::new(Default::default()),
            telemetry: service,
            cors_origin: Some("https://dapp.example".into()),
        })
    }

    async fn call(app: &Router, method: &str, uri: &str, headers: &[(&str, &str)], body: Option<Value>) -> (StatusCode, axum::http::HeaderMap, Value) {
        let mut request = axum::http::Request::builder().method(method).uri(uri);
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        let request = match body {
            Some(body) => request.header("content-type", "application/json").body(axum::body::Body::from(body.to_string())),
            None => request.body(axum::body::Body::empty()),
        }
        .unwrap();
        let response = app.clone().oneshot(request).await.unwrap();
        let (status, headers) = (response.status(), response.headers().clone());
        let bytes = axum::body::to_bytes(response.into_body(), 1 << 20).await.unwrap();
        (status, headers, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
    }

    #[tokio::test]
    async fn telemetry_routes_authenticate_by_signature_and_token() {
        let dir = tempfile::tempdir().unwrap();
        let app = app(dir.path(), true);
        let now = crate::telemetry::now_ms();
        let bearer = format!("Bearer {READ}");
        // Ingress needs no token: the signature authenticates.
        let envelope = fixture::sign(&fixture::snapshot(now, now), &fixture::KEY);
        let (status, _, body) = call(&app, "POST", "/v1/telemetry", &[], Some(json!({ "messages": [envelope.clone(), envelope] }))).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body["results"][0]["status"], "accepted");
        assert_eq!(body["results"][1]["status"], "duplicate");
        let forged = fixture::sign(&fixture::snapshot(now + 1, now), &[8; 32]);
        let (_, _, body) = call(&app, "POST", "/v1/telemetry", &[], Some(json!({ "messages": [forged] }))).await;
        assert_eq!(body["results"][0]["status"], "rejected");
        let many = vec![fixture::sign(&fixture::snapshot(now, now), &fixture::KEY); crate::telemetry::MAX_BATCH + 1];
        assert_eq!(call(&app, "POST", "/v1/telemetry", &[], Some(json!({ "messages": many }))).await.0, StatusCode::BAD_REQUEST);
        // A token does not replace the signature check, and reads still need one.
        assert_eq!(call(&app, "GET", "/v1/health", &[], None).await.0, StatusCode::UNAUTHORIZED);
        let (status, _, body) = call(&app, "GET", "/v1/health", &[("authorization", &bearer)], None).await;
        assert_eq!(status, StatusCode::OK);
        let item = &body["items"][0];
        assert_eq!((item["status"].as_str(), item["codes"].as_array().map(Vec::len)), (Some("healthy"), Some(0)), "{item}");
        assert_eq!(item["report"]["seq"], now);
        assert!(item["report"]["message"].is_string() && item["report"]["signature"].is_string());
        assert_eq!(item["components"][0]["name"], "fw");
        assert!(body["server_time_ms"].as_u64().unwrap() >= now);
        let owner = crate::chain::normalize("0x0a").unwrap();
        let (_, _, summary) = call(&app, "GET", &format!("/v1/health/summary?owner={owner}"), &[("authorization", &bearer)], None).await;
        assert_eq!((summary["total"].as_u64(), summary["counts"]["healthy"].as_u64()), (Some(1), Some(1)), "{summary}");
        let (_, _, summary) = call(&app, "GET", "/v1/health/summary?owner=0x0b", &[("authorization", &bearer)], None).await;
        assert_eq!(summary["total"], 0);
        let device = crate::chain::normalize("0xd1").unwrap();
        let (_, _, history) = call(&app, "GET", &format!("/v1/devices/{device}/telemetry"), &[("authorization", &bearer)], None).await;
        assert_eq!(history["items"].as_array().unwrap().len(), 1);
        assert_eq!(history["next"], Value::Null);
        // CORS only for the configured origin; preflight answered before auth.
        let (status, headers, _) = call(&app, "OPTIONS", "/v1/health", &[("origin", "https://dapp.example")], None).await;
        assert_eq!(status, StatusCode::NO_CONTENT);
        assert_eq!(headers[header::ACCESS_CONTROL_ALLOW_ORIGIN], "https://dapp.example");
        let (_, headers, _) = call(&app, "GET", "/v1/health", &[("origin", "https://dapp.example"), ("authorization", &bearer)], None).await;
        assert_eq!(headers[header::ACCESS_CONTROL_ALLOW_ORIGIN], "https://dapp.example");
        let (status, headers, _) = call(&app, "OPTIONS", "/v1/health", &[("origin", "https://evil.example")], None).await;
        assert!(status != StatusCode::NO_CONTENT && !headers.contains_key(header::ACCESS_CONTROL_ALLOW_ORIGIN));
        // Without a telemetry database the routes say so.
        let disabled = self::app(&dir.path().join("off").tap_dir(), false);
        assert_eq!(call(&disabled, "POST", "/v1/telemetry", &[], Some(json!({ "messages": [] }))).await.0, StatusCode::NOT_FOUND);
        assert_eq!(call(&disabled, "GET", "/v1/health", &[("authorization", &bearer)], None).await.0, StatusCode::NOT_FOUND);
    }

    trait TapDir {
        fn tap_dir(self) -> Self;
    }
    impl TapDir for std::path::PathBuf {
        fn tap_dir(self) -> Self {
            std::fs::create_dir_all(&self).unwrap();
            self
        }
    }

    #[test]
    fn token_scopes_and_file_validation() {
        let tokens = Tokens::parse(&format!(
            "# comment\nread {}\noperate {}\n",
            "r".repeat(32),
            "o".repeat(40)
        ))
        .unwrap();
        assert_eq!(tokens.scope("r".repeat(32).as_bytes()), Some(Scope::Read));
        assert_eq!(
            tokens.scope("o".repeat(40).as_bytes()),
            Some(Scope::Operate)
        );
        assert_eq!(
            tokens.scope("r".repeat(31).as_bytes()),
            None,
            "prefix is not a match"
        );
        assert_eq!(
            tokens.scope(format!("{}x", "r".repeat(32)).as_bytes()),
            None,
            "extension is not a match"
        );
        assert_eq!(tokens.scope(b""), None);
        // Lengths differing by a multiple of 256 must not compare equal.
        for extra in [256, 512, 768] {
            let padded = format!("{}{}", "r".repeat(32), "x".repeat(extra));
            assert_eq!(tokens.scope(padded.as_bytes()), None, "+{extra} bytes");
            let padded = format!("{}{}", "o".repeat(40), "\0".repeat(extra));
            assert_eq!(tokens.scope(padded.as_bytes()), None, "+{extra} NULs");
        }
        assert!(Tokens::parse("admin abc").is_err());
        assert!(Tokens::parse("read short").is_err());
        assert!(Tokens::parse("read").is_err());
    }
}
