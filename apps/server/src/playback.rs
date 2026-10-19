use crate::{engine, state::AppState};
use axum::{extract::State, http::{HeaderValue, StatusCode}, response::{IntoResponse, Response}, Json};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::atomic::Ordering;
use svs_core::{project::TrackKind, timeline::{tick_to_ms, ms_to_tick}, SAMPLE_RATE};

#[derive(Serialize)]
struct TrackPlan { id: String, kind: &'static str, name: String, volume: f32, pan: f32, mute: bool, solo: bool, bus_gain: f32 }
#[derive(Serialize)]
struct PhrasePlan { track_id: String, phrase_index: usize, clip_id: Option<String>, start_tick: u32, end_tick: u32, start_ms: f64, end_ms: f64, channels: u8 }
#[derive(Serialize)]
struct Plan { revision: u64, sample_rate: u32, channels: u8, tempos: Vec<svs_core::timeline::TempoPoint>, end_tick: u32, end_ms: f64, master_volume: f32, tracks: Vec<TrackPlan>, phrases: Vec<PhrasePlan> }

pub async fn plan(State(st): State<AppState>) -> Response {
    let p = st.project.read().await.clone();
    let revision = st.audio_rev.load(Ordering::SeqCst);
    let tempos = p.tempos.clone();
    let mut tracks = Vec::with_capacity(p.tracks.len());
    let mut phrases = Vec::new();
    for t in &p.tracks {
        if t.kind != TrackKind::Sing && t.kind != TrackKind::Instrument && t.kind != TrackKind::Audio { continue; }
        let kind = match t.kind { TrackKind::Sing => "sing", TrackKind::Instrument => "instrument", _ => "audio" };
        tracks.push(TrackPlan { id: t.id.clone(), kind, name: t.name.clone(), volume: t.volume, pan: t.pan, mute: t.mute, solo: t.solo, bus_gain: if kind == "sing" { 1.45 } else if kind == "audio" { 1.0 } else { 0.8 } });
        let parts = engine::phrase_groups(t).iter().map(|ph| {
            let first = ph.iter().map(|n| n.tick).min().unwrap_or(0);
            let last = ph.iter().map(|n| n.tick.saturating_add(n.duration)).max().unwrap_or(first);
            (first, last)
        }).collect::<Vec<_>>();
        for (i, (start, end)) in parts.into_iter().enumerate() {
            phrases.push(PhrasePlan { track_id: t.id.clone(), phrase_index: i, start_tick: start, end_tick: end,
                start_ms: tick_to_ms(&tempos, start), end_ms: tick_to_ms(&tempos, end), channels: 1, clip_id: None });
        }
    }
    for clip in &p.audio_clips {
        let start_ms = tick_to_ms(&tempos, clip.start_tick);
        let duration_ms = crate::media::clip_duration_seconds(&clip.path).map(|s| s * 1000.0).unwrap_or(0.0);
        let end_ms = start_ms + duration_ms;
        let end_tick = ms_to_tick(&tempos, end_ms);
        phrases.push(PhrasePlan { track_id: clip.track_id.clone(), phrase_index: 0, clip_id: Some(clip.id.clone()),
            start_tick: clip.start_tick, end_tick, start_ms, end_ms, channels: 2 });
    }
    phrases.sort_by(|a,b| a.start_ms.total_cmp(&b.start_ms).then_with(|| a.track_id.cmp(&b.track_id)).then_with(|| a.phrase_index.cmp(&b.phrase_index)));
    let end_tick = phrases.iter().map(|ph| ph.end_tick).max().unwrap_or(0).max(p.end_tick());
    Json(Plan { revision, sample_rate: SAMPLE_RATE, channels: 1, tempos, end_tick, end_ms: tick_to_ms(&p.tempos, end_tick), master_volume: p.master_volume, tracks, phrases }).into_response()
}
#[derive(Deserialize)]
pub struct PhraseRequest { revision: u64, track_id: String, phrase_index: usize, #[serde(default)] clip_id: Option<String> }

pub async fn phrase(State(st): State<AppState>, Json(req): Json<PhraseRequest>) -> Response {
    if req.revision != st.audio_rev.load(Ordering::SeqCst) { return stale(); }
    if let Some(clip_id) = req.clip_id {
        let clip = {
            let p = st.project.read().await;
            if req.revision != st.audio_rev.load(Ordering::SeqCst) { return stale(); }
            p.audio_clips.iter().find(|c| c.id == clip_id && c.track_id == req.track_id).cloned()
        };
        let Some(clip) = clip else { return (StatusCode::NOT_FOUND, Json(json!({"error":"NOT_FOUND","detail":"unknown audio clip"}))).into_response(); };
        let response = crate::media::clip_pcm(State(st.clone()), axum::extract::Path(clip.id)).await;
        if req.revision != st.audio_rev.load(Ordering::SeqCst) { return stale(); }
        return response;
    }
    let (track, tempos) = {
        let p = st.project.read().await;
        if req.revision != st.audio_rev.load(Ordering::SeqCst) { return stale(); }
        match p.tracks.iter().find(|t| t.id == req.track_id) {
            Some(t) if t.kind == TrackKind::Sing || t.kind == TrackKind::Instrument => (t.clone(), p.tempos.clone()),
            _ => return (StatusCode::NOT_FOUND, Json(json!({"error":"NOT_FOUND","detail":"unknown playable track"}))).into_response(),
        }
    };
    let phrases = engine::phrase_groups(&track);
    let Some(notes) = phrases.get(req.phrase_index) else {
        return (StatusCode::NOT_FOUND, Json(json!({"error":"NOT_FOUND","detail":"unknown phrase"}))).into_response();
    };
    let timbre = if track.kind == TrackKind::Instrument {
        Some(if track.instrument.contains("drum") { "drum" } else if track.name.contains("贝斯") || track.instrument.contains("bass") { "bass" } else { "piano" })
    } else { None };
    let native = engine::is_native_id(&track.singer_id);
    let built = engine::phrase_spec(&track, notes, &tempos, native, timbre);
    let _gate = st.render_gate.lock().await;
    if req.revision != st.audio_rev.load(Ordering::SeqCst) { return stale(); }
    if let Err(e) = engine::ensure_audio_service(&st).await {
        return (StatusCode::BAD_GATEWAY, Json(json!({"error":"SYNTH_FAILED","detail":e.to_string()}))).into_response();
    }
    let pcm = match engine::fetch_audio(&st, &built.payload, built.endpoint, if native { 1800 } else { 120 }).await {
        Ok(pcm) => pcm,
        Err(e) => return (StatusCode::BAD_GATEWAY, Json(json!({"error":"SYNTH_FAILED","detail":e.to_string()}))).into_response(),
    };
    if req.revision != st.audio_rev.load(Ordering::SeqCst) { return stale(); }
    let mut bytes = Vec::with_capacity(pcm.len() * 4);
    for sample in pcm { bytes.extend_from_slice(&sample.to_le_bytes()); }
    Response::builder().status(StatusCode::OK).header("Content-Type", "application/octet-stream").header("X-Sample-Rate", HeaderValue::from_static("24000")).header("X-Channels", HeaderValue::from_static("1"))
        .body(axum::body::Body::from(bytes)).unwrap()
}
fn stale() -> Response { (StatusCode::CONFLICT, Json(json!({"error":"STALE_PLAYBACK"}))).into_response() }

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn stale_revision_rejected_before_track_lookup() {
        let st = AppState::new();
        let response = phrase(State(st), Json(PhraseRequest { revision: 99, track_id: "missing".into(), phrase_index: 0, clip_id: None })).await;
        assert_eq!(response.status(), StatusCode::CONFLICT);
    }

    #[tokio::test]
    async fn plan_preserves_muted_polyphony_and_tempo_timing() {
        let st = AppState::new();
        let mut track = svs_core::project::Track::new(TrackKind::Instrument, "muted piano");
        let id = track.id.clone();
        track.mute = true;
        track.notes = vec![
            svs_core::phrase::Note { id: "long".into(), tick: 0, duration: 1920, key: 48, velocity: 1.0, lyric: "".into(), phonemes: vec![], legato: false, head_ticks: 0, tail_ticks: 0, kind: "sung".into(), vibrato: None, attack_ms: None, decay_ms: None },
            svs_core::phrase::Note { id: "short".into(), tick: 240, duration: 120, key: 64, velocity: 1.0, lyric: "".into(), phonemes: vec![], legato: false, head_ticks: 0, tail_ticks: 0, kind: "sung".into(), vibrato: None, attack_ms: None, decay_ms: None },
        ];
        {
            let mut project = st.project.write().await;
            project.tempos.push(svs_core::timeline::TempoPoint { tick: 960, bpm: 60.0, curve: svs_core::timeline::TempoCurve::Step });
            project.tracks.push(track);
        }
        let response = plan(State(st.clone())).await;
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(body["tracks"][0]["mute"], true);
        assert_eq!(body["phrases"][0]["start_tick"], 0);
        assert_eq!(body["phrases"][0]["end_tick"], 1920);
        assert_eq!(body["phrases"][0]["end_ms"], 3000.0);
        let response = phrase(State(st), Json(PhraseRequest { revision: 0, track_id: id, phrase_index: 1, clip_id: None })).await;
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }
}

