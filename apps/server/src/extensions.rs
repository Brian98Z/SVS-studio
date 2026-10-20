//! Same-origin bridge for local inference extensions; explicit bounded route allowlist.
use axum::{body::Bytes, extract::{Path, State}, http::{header, HeaderMap, Method, StatusCode}, response::{IntoResponse, Response}, Json};
use crate::{engine, state::AppState};
use serde_json::json;

pub async fn proxy(State(st): State<AppState>, Path(path): Path<String>, method: Method, headers: HeaderMap, body: Bytes) -> Response {
    let allowed = ["voices", "vst", "media", "music-ai", "ai", "expression", "mix", "g2p", "hardware", "sync"];
    if !allowed.contains(&path.split('/').next().unwrap_or("")) || path.contains("..") {
        return (StatusCode::NOT_FOUND, Json(json!({"error":"NOT_FOUND"}))).into_response();
    }
    let native_realtime = path.starts_with("vst/realtime/");
    if (path.split('/').next() == Some("sync") || native_realtime) && method != Method::GET
        && !headers.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.split(';').next().unwrap_or("").trim().eq_ignore_ascii_case("application/json")) {
        return (StatusCode::UNSUPPORTED_MEDIA_TYPE, Json(json!({"error":"BAD_REQUEST","detail":"Requests require application/json"}))).into_response();
    }
    if let Err(e) = engine::ensure_audio_service(&st).await {
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error":"INFERENCE_DOWN","detail":e.to_string()}))).into_response();
    }
    let Some(base) = st.inference_url.read().await.clone() else { return StatusCode::SERVICE_UNAVAILABLE.into_response(); };
    let response = st.http.request(method, format!("{base}/{path}"))
        .header("Content-Type", "application/json").body(body)
        .timeout(std::time::Duration::from_secs(if native_realtime { 10 } else { 1800 })).send().await;
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
