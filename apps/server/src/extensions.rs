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
    let binary_process = path == "vst/realtime/process";
    if (path.split('/').next() == Some("sync") || native_realtime) && method != Method::GET {
        let expected = if binary_process { "application/octet-stream" } else { "application/json" };
        if !headers.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok())
            .is_some_and(|v| v.split(';').next().unwrap_or("").trim().eq_ignore_ascii_case(expected)) {
            return (StatusCode::UNSUPPORTED_MEDIA_TYPE, Json(json!({"error":"BAD_REQUEST","detail":format!("Requests require {expected}")}))).into_response();
        }
    }
    if let Err(e) = engine::ensure_audio_service(&st).await {
        return (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error":"INFERENCE_DOWN","detail":e.to_string()}))).into_response();
    }
    let Some(base) = st.inference_url.read().await.clone() else { return StatusCode::SERVICE_UNAVAILABLE.into_response(); };
    let mut upstream = st.http.request(method, format!("{base}/{path}"));
    if let Some(content_type) = headers.get(header::CONTENT_TYPE) { upstream = upstream.header(header::CONTENT_TYPE, content_type); }
    for name in ["x-audio-session-id", "x-audio-sequence"] {
        if let Some(value) = headers.get(name) { upstream = upstream.header(name, value); }
    }
    let response = upstream.body(body)
        .timeout(std::time::Duration::from_secs(if native_realtime { 10 } else { 1800 })).send().await;
    match response {
        Ok(r) => {
            let status = r.status();
            let response_headers = r.headers().clone();
            match r.bytes().await {
                Ok(bytes) => {
                    let mut response = (status, bytes).into_response();
                    for name in ["content-type", "x-audio-sequence", "x-audio-frames", "x-audio-processing-ms"] {
                        if let Some(value) = response_headers.get(name) { response.headers_mut().insert(name, value.clone()); }
                    }
                    response
                }
                Err(e) => (StatusCode::BAD_GATEWAY, Json(json!({"error":"INFERENCE_RESPONSE","detail":e.to_string()}))).into_response(),
            }
        }
        Err(e) => (StatusCode::BAD_GATEWAY, Json(json!({"error":"INFERENCE_REQUEST","detail":e.to_string()}))).into_response(),
    }
}
