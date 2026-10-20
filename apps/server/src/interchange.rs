//! Format interchange endpoint backed by the stdlib-only Python bridge.
use crate::{engine, state::AppState};
use axum::{extract::State, response::IntoResponse, Json};
use serde_json::{json, Value};

/// POST /api/interchange. Import actions replace the current project only after
/// both the bridge and core tempo validation succeed. Export actions default to
/// a snapshot of the active project when no explicit project is supplied.
pub async fn interchange(State(st): State<AppState>, Json(mut request): Json<Value>) -> axum::response::Response {
    let Some(action) = request.get("action").and_then(Value::as_str).map(str::to_owned) else {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"ok":false,"error":"action is required"}))).into_response();
    };
    let importing = matches!(action.as_str(), "import_musicxml" | "import_midi" | "import_ufdata" | "import_bundle" | "sync_pull");
    let exporting = matches!(action.as_str(), "export_musicxml" | "export_midi" | "export_lrc" | "export_bundle" | "sync_push" | "sync_pull");
    if !importing && !exporting {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"ok":false,"error":"unsupported action"}))).into_response();
    }
    let sync_revision;
    {
        let current = st.project.read().await;
        sync_revision = st.edit_rev.load(std::sync::atomic::Ordering::SeqCst);
        if exporting && request.get("project").is_none() {
            request["project"] = match serde_json::to_value(&*current) {
                Ok(project) => project,
                Err(e) => return (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"ok":false,"error":e.to_string()}))).into_response(),
            };
        }
    }
    if let Err(e) = engine::ensure_audio_service(&st).await {
        return (axum::http::StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error":"INFERENCE_DOWN","detail":e.to_string()}))).into_response();
    }
    let Some(base) = st.inference_url.read().await.clone() else { return axum::http::StatusCode::SERVICE_UNAVAILABLE.into_response(); };
    let result = st.http.post(format!("{base}/interchange")).json(&request)
        .timeout(std::time::Duration::from_secs(120)).send().await;
    let response: Value = match result {
        Ok(r) => {
            let status = r.status();
            let value = match r.json::<Value>().await {
                Ok(v) => v,
                Err(e) => return (axum::http::StatusCode::BAD_GATEWAY, Json(json!({"error":"INTERCHANGE_RESPONSE","detail":e.to_string()}))).into_response(),
            };
            if !status.is_success() { return (status, Json(value)).into_response(); }
            value
        }
        Err(e) => return (axum::http::StatusCode::BAD_GATEWAY, Json(json!({"error":"INTERCHANGE_FAILED","detail":e.to_string()}))).into_response(),
    };
    if importing {
        let project: svs_core::project::Project = match serde_json::from_value(response["project"].clone()) {
            Ok(p) => p,
            Err(e) => return (axum::http::StatusCode::BAD_GATEWAY, Json(json!({"ok":false,"error":format!("bridge returned invalid project: {e}")}))).into_response(),
        };
        if let Err(e) = svs_core::timeline::validate_tempos(&project.tempos) {
            return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"ok":false,"error":format!("invalid imported tempo map: {e}")}))).into_response();
        }
        let mut current = st.project.write().await;
        if action == "sync_pull" && sync_revision != st.edit_rev.load(std::sync::atomic::Ordering::SeqCst) {
            return (axum::http::StatusCode::CONFLICT, Json(json!({"error":"STALE_PROJECT","detail":"工程在下载期间已编辑；未替换当前工程，已下载文件和备份仍保留。","project_path":response.get("project_path"),"backup_path":response.get("backup_path")}))).into_response();
        }
        st.history.lock().await.push(&*current);
        *current = project;
        *st.path.write().await = if matches!(action.as_str(), "import_bundle" | "sync_pull") {
            response.get("project_path").and_then(Value::as_str).map(str::to_owned)
        } else {
            None
        };
        st.bump_rev();
        drop(current);
        st.broadcast(json!({"type":"project.imported","action":action})).await;
    }
    Json(response).into_response()
}
