//! Versioned HTTP API (`/v1`) over the materialized fleet state, plus
//! unsigned bulk-operation transactions. The service never holds keys.
//!
//! Auth: bearer tokens from a file of `read|operate TOKEN` lines, compared in
//! constant time. `operate` covers `read`. With `public_read`, GET requests
//! need no token (for local use only).
use crate::chain::Chain;
use crate::store::{self, Filter, Readers};
use crate::txbuild;
use axum::{
    extract::{Path, Query, Request, State},
    http::{header, Method, StatusCode},
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
    pub types: Arc<std::collections::BTreeMap<String, String>>,
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
    let (rows, chain_identifier, last_change) = blocking(&state.readers, |db| {
        Ok((
            store::cursors(db)?,
            store::meta(db, "chain_identifier")?,
            store::last_change(db)?,
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
    let readers = state.readers.clone();
    let (types, package) = (state.types.clone(), state.call_package.clone());
    let operation_name = operation.clone();
    let planned = blocking(&readers, move |db| {
        txbuild::plan(db, &types, &package, &operation_name, &request)
    })
    .await?;
    let built = txbuild::build(&state.chain, planned).await?;
    Ok(Json(built))
}

pub fn router(state: AppState) -> Router {
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
        .layer(middleware::from_fn_with_state(state.clone(), auth))
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;

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
