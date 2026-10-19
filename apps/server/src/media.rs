//! Local audio/video import and interchange HTTP handlers.
use axum::{extract::State, http::StatusCode, response::{IntoResponse, Response}, Json};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{path::{Path, PathBuf}, process::Command};
use svs_core::project::{AudioClip, Track, TrackKind};
use crate::state::AppState;

#[derive(Deserialize)] pub struct ImportReq { pub path: String, pub track_id: Option<String>, pub start_tick: Option<u32> }
#[derive(Deserialize)] pub struct ProbeReq { pub path: String }
#[derive(Deserialize)] pub struct ExportReq { pub output_path: String }
#[derive(Deserialize)] pub struct AssembleReq { pub video_path: String, pub audio_path: String, pub output_path: String }

fn err(code: StatusCode, detail: impl ToString) -> Response { (code, Json(json!({"error":"MEDIA_ERROR","detail":detail.to_string()}))).into_response() }
fn allowed(path: &str) -> Result<PathBuf, String> {
    let p=Path::new(path).canonicalize().map_err(|e|e.to_string())?;
    if !p.is_file() { return Err("path must be a regular local file".into()); }
    let ext=p.extension().and_then(|x|x.to_str()).unwrap_or("").to_ascii_lowercase();
    if !["wav","wave","mp3","flac","ogg","oga","m4a","aac","opus","aiff","aif","mp4","mov","mkv","webm","avi","m4v","mpeg","mpg"].contains(&ext.as_str()) { return Err(format!("unsupported media extension: {ext}")); }
    Ok(p)
}
fn probe(path: &Path) -> Result<Value,String> {
    let out=Command::new("ffprobe").args(["-v","error","-show_format","-show_streams","-of","json"]).arg(path).output().map_err(|e|e.to_string())?;
    if !out.status.success() { return Err(String::from_utf8_lossy(&out.stderr).into_owned()); }
    serde_json::from_slice(&out.stdout).map_err(|e|e.to_string())
}
/// Probe media duration without decoding audio samples.
pub fn clip_duration_seconds(path: &str) -> Result<f64, String> {
    let source = allowed(path)?;
    let meta = probe(&source)?;
    let duration = meta["streams"].as_array().and_then(|streams| streams.iter().find(|s| s["codec_type"] == "audio"))
        .and_then(|audio| audio["duration"].as_str()).and_then(|s| s.parse::<f64>().ok())
        .or_else(|| meta["format"]["duration"].as_str().and_then(|s| s.parse::<f64>().ok()))
        .filter(|d| d.is_finite() && *d > 0.0).ok_or_else(|| "audio duration unavailable".to_string())?;
    Ok(duration)
}
pub async fn probe_media(Json(req): Json<ProbeReq>) -> Response {
    let path=match allowed(&req.path) { Ok(p)=>p,Err(e)=>return err(StatusCode::UNPROCESSABLE_ENTITY,e) };
    match probe(&path) { Ok(v)=>Json(json!({"path":path,"probe":v})).into_response(),Err(e)=>err(StatusCode::UNPROCESSABLE_ENTITY,e) }
}
/// Import registers media as a duration-bearing audio clip on an existing or new audio track.
pub async fn import_audio(State(st): State<AppState>, Json(req): Json<ImportReq>) -> Response {
    let path=match allowed(&req.path) { Ok(p)=>p,Err(e)=>return err(StatusCode::UNPROCESSABLE_ENTITY,e) };
    let meta=match probe(&path) { Ok(v)=>v,Err(e)=>return err(StatusCode::UNPROCESSABLE_ENTITY,e) };
    let aud=meta["streams"].as_array().and_then(|ss|ss.iter().find(|s|s["codec_type"]=="audio"));
    let Some(aud)=aud else{return err(StatusCode::UNPROCESSABLE_ENTITY,"file has no audio stream")};
    let duration=aud["duration"].as_str().and_then(|s|s.parse::<f64>().ok()).or_else(||meta["format"]["duration"].as_str().and_then(|s|s.parse::<f64>().ok())).unwrap_or(0.0);
    if !duration.is_finite() || duration<=0.0 {return err(StatusCode::UNPROCESSABLE_ENTITY,"audio duration is unavailable")}
    let (track_id, clip_id);
    st.snapshot().await;
    {
        let mut p=st.project.write().await;
        track_id=if let Some(id)=req.track_id { if !p.tracks.iter().any(|t|t.id==id && t.kind==TrackKind::Audio) {return err(StatusCode::BAD_REQUEST,"track_id must identify an audio track")}; id }
        else {let t=Track::new(TrackKind::Audio,"Audio");let id=t.id.clone();p.tracks.push(t);id};
        clip_id=uuid::Uuid::new_v4().to_string();
        p.audio_clips.push(AudioClip{id:clip_id.clone(),track_id:track_id.clone(),path:path.to_string_lossy().into_owned(),start_tick:req.start_tick.unwrap_or(0),gain:1.0,fade_in_ms:0.0,fade_out_ms:0.0});
        st.bump_rev();
    }
    Json(json!({"ok":true,"track_id":track_id,"clip":{"id":clip_id,"track_id":track_id,"path":path,"start_tick":req.start_tick.unwrap_or(0),"gain":1.0,"fade_in_ms":0.0,"fade_out_ms":0.0,"duration_seconds":duration},"probe":meta})).into_response()
}
pub async fn export_fcpxml(State(st): State<AppState>, Json(req): Json<ExportReq>) -> Response { export_proxy(st,"export_fcpxml",req.output_path).await }
pub async fn export_aaf(State(st): State<AppState>, Json(req): Json<ExportReq>) -> Response { export_proxy(st,"export_aaf",req.output_path).await }
async fn export_proxy(st: AppState, operation: &str, output_path: String) -> Response {
    let path=Path::new(&output_path); if path.exists() || !path.parent().map(Path::is_dir).unwrap_or(false) {return err(StatusCode::BAD_REQUEST,"output must be a new file in an existing local directory")}
    let project=st.project.read().await.clone();
    let base=st.inference_url.read().await.clone().unwrap_or_else(||"http://127.0.0.1:8760".into());
    let parent=st.http.post("http://127.0.0.1:8761/api/render/stems").json(&json!({"directory":std::env::temp_dir().join(format!("svs-stems-{}",uuid::Uuid::new_v4()))})).send().await;
    let stems=match parent {Ok(r) if r.status().is_success()=>r.json::<Value>().await.ok().and_then(|v|v.get("stems").cloned()).unwrap_or(json!([])),Ok(r)=>return err(StatusCode::BAD_GATEWAY,format!("stem render failed: {}",r.status())),Err(e)=>return err(StatusCode::BAD_GATEWAY,e)};
    match st.http.post(format!("{base}/media/{operation}")).json(&json!({"project":project,"stems":stems,"output_path":output_path})).send().await {
      Ok(r)=>{let status=r.status(); match r.json::<Value>().await {Ok(v)=>(StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),Json(v)).into_response(),Err(e)=>err(StatusCode::BAD_GATEWAY,e)}},Err(e)=>err(StatusCode::SERVICE_UNAVAILABLE,e)
    }
}
pub async fn assemble_video(State(st): State<AppState>, Json(req): Json<AssembleReq>) -> Response {
 let base=st.inference_url.read().await.clone().unwrap_or_else(||"http://127.0.0.1:8760".into());
 match st.http.post(format!("{base}/media/assemble_video")).json(&json!({"video_path":req.video_path,"audio_path":req.audio_path,"output_path":req.output_path})).send().await {
  Ok(r)=>{let status=r.status();match r.json::<Value>().await{Ok(v)=>(StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),Json(v)).into_response(),Err(e)=>err(StatusCode::BAD_GATEWAY,e)}},Err(e)=>err(StatusCode::SERVICE_UNAVAILABLE,e)
 }
}
/// Decode one imported project clip as stereo interleaved float32 little-endian PCM.
pub async fn clip_pcm(State(st): State<AppState>, axum::extract::Path(id): axum::extract::Path<String>) -> Response {
    let project=st.project.read().await.clone();
    let Some(clip)=project.audio_clips.iter().find(|c| c.id==id) else{return err(StatusCode::NOT_FOUND,"audio clip not found")};
    let source=match allowed(&clip.path) {Ok(p)=>p,Err(e)=>return err(StatusCode::UNPROCESSABLE_ENTITY,e)};
    let meta=match probe(&source) {Ok(m)=>m,Err(e)=>return err(StatusCode::UNPROCESSABLE_ENTITY,e)};
    let duration=meta["streams"].as_array().and_then(|s|s.iter().find(|v|v["codec_type"]=="audio")).and_then(|a|a["duration"].as_str()).and_then(|s|s.parse::<f64>().ok()).or_else(||meta["format"]["duration"].as_str().and_then(|s|s.parse::<f64>().ok())).unwrap_or(0.0);
    if duration<=0.0 || !duration.is_finite(){return err(StatusCode::UNPROCESSABLE_ENTITY,"audio duration unavailable")}
    let output=match tokio::task::spawn_blocking(move || Command::new("ffmpeg").args(["-nostdin","-v","error","-i"]).arg(&source).args(["-map","0:a:0","-vn","-ac","2","-ar"]).arg(project.sample_rate.to_string()).args(["-f","f32le","pipe:1"]).output()).await {
        Ok(Ok(out)) if out.status.success()=>out,
        Ok(Ok(out))=>return err(StatusCode::UNPROCESSABLE_ENTITY,String::from_utf8_lossy(&out.stderr)),
        Ok(Err(e))=>return err(StatusCode::SERVICE_UNAVAILABLE,e),
        Err(e)=>return err(StatusCode::INTERNAL_SERVER_ERROR,e),
    };
    let bytes=output.stdout;
    if bytes.len()%8!=0{return err(StatusCode::INTERNAL_SERVER_ERROR,"decoded PCM has incomplete stereo frame")}
    let frames=bytes.len()/8;
    let mut response=Response::new(axum::body::Body::from(bytes));
    *response.status_mut()=StatusCode::OK;
    let headers=response.headers_mut(); headers.insert(axum::http::header::CONTENT_TYPE,axum::http::HeaderValue::from_static("application/octet-stream"));
    for (name,value) in [("x-sample-rate",project.sample_rate.to_string()),("x-channels","2".into()),("x-frames",frames.to_string()),("x-start-tick",clip.start_tick.to_string()),("x-duration-seconds",duration.to_string()),("x-gain",clip.gain.to_string()),("x-fade-in-ms",clip.fade_in_ms.to_string()),("x-fade-out-ms",clip.fade_out_ms.to_string()),("x-track-id",clip.track_id.clone())] {if let Ok(v)=axum::http::HeaderValue::from_str(&value){headers.insert(name,v);}}
    response
}
