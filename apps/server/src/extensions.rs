//! Same-origin bridge for local inference extensions; explicit bounded route allowlist.
use axum::{body::Bytes, extract::{Path, State}, http::{Method, StatusCode}, response::{IntoResponse, Response}, Json};
use crate::{engine, state::AppState};
use serde_json::json;

pub async fn proxy(State(st): State<AppState>, Path(path): Path<String>, method: Method, body: Bytes) -> Response {
    let allowed = ["voices", "vst", "media", "music-ai", "ai", "expression", "mix", "g2p", "hardware"];
    if !allowed.contains(&path.split('/').next().unwrap_or("")) || path.contains("..") {
        return (StatusCode::NOT_FOUND, Json(json!({"error":"NOT_FOUND"}))).into_response();
    }
    if let Err(e) = engine::ensure_audio_service(&st).await {
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error":"INFERENCE_DOWN","detail":e.to_string()}))).into_response();
    }
    let Some(base) = st.inference_url.read().await.clone() else { return StatusCode::SERVICE_UNAVAILABLE.into_response(); };
    let response = st.http.request(method, format!("{base}/{path}"))
        .header("Content-Type", "application/json").body(body)
        .timeout(std::time::Duration::from_secs(1800)).send().await;
    match response {
        Ok(r) => {
            let status = r.status();
            let content_type = r.headers().get("content-type").cloned();
            match r.bytes().await {
                Ok(bytes) => {
                    let mut response = (status, bytes).into_response();
                    if let Some(value) = content_type { response.headers_mut().insert("content-type", value); }
                    response
                }
                Err(e) => (StatusCode::BAD_GATEWAY, Json(json!({"error":"INFERENCE_RESPONSE","detail":e.to_string()}))).into_response(),
            }
        }
        Err(e) => (StatusCode::BAD_GATEWAY, Json(json!({"error":"INFERENCE_REQUEST","detail":e.to_string()}))).into_response(),
    }
}
