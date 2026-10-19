//! Tempo-map mutations.
use crate::state::AppState;
use axum::{extract::State, http::StatusCode, response::{IntoResponse, Response}, Json};
use serde::Deserialize;
use serde_json::json;
use svs_core::{timeline::{validate_tempos, TempoPoint}, PPQ};

#[derive(Deserialize)]
pub struct TempoMapReq { pub tempos: Vec<TempoPoint> }

pub async fn patch_tempos(State(st): State<AppState>, Json(req): Json<TempoMapReq>) -> Response {
    if let Err(error) = validate_tempos(&req.tempos) {
        return (StatusCode::BAD_REQUEST, Json(json!({"error":"BAD_REQUEST", "detail":error}))).into_response();
    }
    st.snapshot().await;
    {
        let mut project = st.project.write().await;
        project.tempos = req.tempos;
        st.bump_rev();
    }
    let tempos = st.project.read().await.tempos.clone();
    (StatusCode::OK, Json(json!({"ok":true, "tempos":tempos}))).into_response()
}
