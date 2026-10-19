use crate::{engine, state::AppState};
use axum::{extract::State, response::{IntoResponse,Response}, Json, http::StatusCode};
use serde::Deserialize;
use serde_json::json;

#[derive(Deserialize)]
pub struct MixerPatch { mixer: serde_json::Value }

pub async fn patch_mixer(State(st):State<AppState>,Json(req):Json<MixerPatch>)->Response {
    if let Err(e)=engine::ensure_audio_service(&st).await {
        return (StatusCode::SERVICE_UNAVAILABLE,Json(json!({"error":"INFERENCE_DOWN","detail":e.to_string()}))).into_response();
    }
    let Some(base)=st.inference_url.read().await.clone() else{return StatusCode::SERVICE_UNAVAILABLE.into_response()};
    let response=st.http.post(format!("{base}/mix/validate")).json(&req.mixer).send().await;
    let normalized=match response {
        Ok(r)=>{
            let status=r.status();
            let data=r.json::<serde_json::Value>().await.unwrap_or(json!({"error":"INVALID_MIXER_RESPONSE"}));
            if !status.is_success(){return (status,Json(data)).into_response()}
            data.get("mixer").cloned().unwrap_or(req.mixer)
        }
        Err(e)=>return (StatusCode::BAD_GATEWAY,Json(json!({"error":"MIXER_VALIDATION","detail":e.to_string()}))).into_response(),
    };
    st.snapshot().await;
    let mut p=st.project.write().await;
    if let Some(volume)=normalized.pointer("/master/volume").and_then(|v|v.as_f64()) {p.master_volume=volume.clamp(0.,1.) as f32;}
    p.mixer=normalized;
    st.bump_mix_rev();
    Json(json!({"ok":true,"mixer":p.mixer,"master_volume":p.master_volume})).into_response()
}
