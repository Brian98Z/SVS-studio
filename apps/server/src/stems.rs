//! Render each audible project track through the shared stereo mixer into a separate WAV stem.
use crate::{rendering,state::AppState};
use axum::{extract::State,response::IntoResponse,Json};
use serde::Deserialize;
use serde_json::json;
use std::{path::{Path,PathBuf},sync::atomic::Ordering};
use svs_core::{project::TrackKind,PPQ,SAMPLE_RATE};

#[derive(Deserialize)]
pub struct StemRequest{pub directory:String}
fn safe_name(s:&str)->String{let n:String=s.chars().map(|c|if c.is_ascii_alphanumeric()||matches!(c,'-'|'_'|'.'){c}else{'_'}).collect();if n.is_empty()||n=="."||n==".."{"track".into()}else{n}}
fn write_wav(path:&Path,samples:&[f32])->Result<(),String>{
    let file=std::fs::OpenOptions::new().write(true).create_new(true).open(path).map_err(|e|format!("{}: {e}",path.display()))?;
    let spec=hound::WavSpec{channels:2,sample_rate:SAMPLE_RATE,bits_per_sample:32,sample_format:hound::SampleFormat::Float};
    let mut writer=hound::WavWriter::new(std::io::BufWriter::new(file),spec).map_err(|e|e.to_string())?;
    for sample in samples{writer.write_sample(*sample).map_err(|e|e.to_string())?;}
    writer.finalize().map_err(|e|e.to_string())
}
pub async fn export_stems(State(st):State<AppState>,Json(req):Json<StemRequest>)->axum::response::Response{
    let _gate=match st.render_gate.try_lock(){Ok(g)=>g,Err(_)=>return(axum::http::StatusCode::CONFLICT,Json(json!({"error":"RENDER_BUSY"}))).into_response()};
    if let Err(e)=crate::engine::ensure_audio_service(&st).await{return(axum::http::StatusCode::SERVICE_UNAVAILABLE,Json(json!({"error":"SYNTH_FAILED","detail":e.to_string()}))).into_response()}
    let outdir=PathBuf::from(&req.directory);if let Err(e)=std::fs::create_dir_all(&outdir){return(axum::http::StatusCode::BAD_REQUEST,Json(json!({"error":"INVALID_DIRECTORY","detail":e.to_string()}))).into_response()}
    let(project,rev)={let p=st.project.read().await;(p.clone(),st.edit_rev.load(Ordering::SeqCst))};
    let tracks:Vec<_>=project.tracks.iter().filter(|t|matches!(t.kind,TrackKind::Sing|TrackKind::Instrument|TrackKind::Audio)&&project.track_audible(t)&&(!t.notes.is_empty()||project.audio_clips.iter().any(|c|c.track_id==t.id))).cloned().collect();
    if tracks.is_empty(){return(axum::http::StatusCode::BAD_REQUEST,Json(json!({"error":"NO_AUDIBLE_TRACKS"}))).into_response()}
    let mut files=Vec::with_capacity(tracks.len());let mut used=std::collections::HashSet::new();
    for track in &tracks{
        if rev!=st.edit_rev.load(Ordering::SeqCst){return(axum::http::StatusCode::CONFLICT,Json(json!({"error":"STALE_MIX"}))).into_response()}
        let mut solo=project.clone();solo.tracks.retain(|t|t.id==track.id);solo.audio_clips.retain(|c|c.track_id==track.id);solo.master_volume=1.0;
        if let Some(master)=solo.mixer.get_mut("master").and_then(serde_json::Value::as_object_mut){master.insert("effects".into(),json!([]));master.insert("volume".into(),json!(1.0));}
        let pcm=match rendering::mix_project(&st,&solo).await{Ok(pcm)=>pcm,Err(e)=>return(axum::http::StatusCode::BAD_GATEWAY,Json(json!({"error":"STEM_RENDER_FAILED","track":track.id,"detail":e.to_string()}))).into_response()};
        if pcm.len()%2!=0{return(axum::http::StatusCode::BAD_GATEWAY,Json(json!({"error":"MIXER_RETURNED_INVALID_STEREO","track":track.id}))).into_response()}
        if rev!=st.edit_rev.load(Ordering::SeqCst){return(axum::http::StatusCode::CONFLICT,Json(json!({"error":"STALE_MIX"}))).into_response()}
        let mut name=safe_name(&track.name);if !used.insert(name.clone()){name=format!("{}-{}",name,safe_name(&track.id));used.insert(name.clone());}
        let path=outdir.join(format!("{name}.wav"));if let Err(e)=write_wav(&path,&pcm){return(axum::http::StatusCode::CONFLICT,Json(json!({"error":"STEM_WRITE_FAILED","detail":e}))).into_response()}
        files.push(json!({"track_id":track.id,"track_name":track.name,"path":path,"samples":pcm.len()/2,"channels":2,"sample_rate":SAMPLE_RATE,"includes_track_fx":true,"includes_audio_clips":track.kind==TrackKind::Audio}));
    }
    if rev!=st.edit_rev.load(Ordering::SeqCst){return(axum::http::StatusCode::CONFLICT,Json(json!({"error":"STALE_MIX"}))).into_response()}
    Json(json!({"ok":true,"directory":outdir,"stems":files,"revision":rev,"ppq":PPQ})).into_response()
}
