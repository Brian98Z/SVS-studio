//! REST handlers（契约 §6/§7）+ WS 推送 + WAV 并轨。

use crate::engine;
use crate::state::AppState;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::response::IntoResponse;
use axum::Json;
use serde::Deserialize;
use serde_json::json;
use svs_core::curve::ParamCurve;
use svs_core::phrase::Note;
use svs_core::project::{Track, TrackKind};
use svs_core::{PPQ, SAMPLE_RATE};

fn ok(v: serde_json::Value) -> axum::response::Response {
    Json(v).into_response()
}

// ---------- 工程 ----------
pub async fn get_project(State(st): State<AppState>) -> axum::response::Response {
    let p = st.project.read().await;
    ok(json!({
        "schema": p.schema, "name": p.name, "ppq": p.ppq,
        "sample_rate": p.sample_rate, "tempos": p.tempos,
        "master_volume": p.master_volume, "metronome": p.metronome,
        "metronome_level": p.metronome_level,
        "time_signatures": p.time_signatures, "tracks": p.tracks,
        "audio_clips": p.audio_clips,
        "mixer": p.mixer,
    }))
}

pub async fn new_project(State(st): State<AppState>) -> axum::response::Response {
    st.snapshot().await;
    {
        let mut p = st.project.write().await;
        *p = svs_core::project::Project::new("未命名工程");
        st.bump_rev();
    }
    *st.path.write().await = None;
    ok(json!({"ok": true}))
}

#[derive(Deserialize)]
pub struct OpenReq {
    pub path: String,
}

pub async fn open_project(State(st): State<AppState>, Json(req): Json<OpenReq>) -> axum::response::Response {
    match std::fs::read_to_string(&req.path) {
        Ok(s) => match svs_core::project::Project::from_json(&s).and_then(|p| {
            svs_core::timeline::validate_tempos(&p.tempos)
                .map_err(|message| serde_json::Error::io(std::io::Error::new(std::io::ErrorKind::InvalidData, message)))?;
            Ok(p)
        }) {
            Ok(p) => {
                st.snapshot().await;
                {
                    let mut current = st.project.write().await;
                    *current = p;
                    st.bump_rev();
                }
                *st.path.write().await = Some(req.path.clone());
                ok(json!({"ok": true, "path": req.path})).into_response()
            }
            Err(e) => (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "BAD_REQUEST", "detail": e.to_string()}))).into_response(),
        },
        Err(e) => (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": e.to_string()}))).into_response(),
    }
}

pub async fn save_project(State(st): State<AppState>, req: Option<Json<OpenReq>>) -> axum::response::Response {
    let path = req
        .and_then(|Json(r)| Some(r.path))
        .or_else(|| {
            // 同步读 lock 会阻塞 runtime；用 try_read
            st.path.try_read().ok().and_then(|p| p.clone())
        })
        .unwrap_or_else(|| "untitled.svsproj".into());
    let p = st.project.read().await;
    let s = p.to_json();
    match std::fs::write(&path, s) {
        Ok(_) => {
            *st.path.write().await = Some(path.clone());
            st.broadcast(json!({"type": "project.saved", "path": path})).await;
            ok(json!({"ok": true, "path": path}))
        }
        Err(e) => (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "INTERNAL", "detail": e.to_string()}))).into_response(),
    }
}

// ---------- 轨道 ----------
#[derive(Deserialize)]
pub struct AddTrackReq {
    pub kind: String,
    #[serde(default)]
    pub name: String,
}

pub async fn add_track(State(st): State<AppState>, Json(req): Json<AddTrackReq>) -> axum::response::Response {
    st.snapshot().await;
    let kind = match req.kind.as_str() {
        "sing" => TrackKind::Sing,
        "instrument" => TrackKind::Instrument,
        "audio" => TrackKind::Audio,
        "chord" => TrackKind::Chord,
        _ => return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "BAD_REQUEST", "detail": "kind"}))).into_response(),
    };
    let name = if req.name.is_empty() { req.kind.clone() } else { req.name };
    let t = Track::new(kind, &name);
    let id = t.id.clone();
    let mut p = st.project.write().await;
    p.tracks.push(t);
    st.bump_rev();
    drop(p);
    ok(json!({"ok": true, "id": id})).into_response()
}

#[derive(Deserialize)]
pub struct PatchTrackReq {
    #[serde(default)] pub name: Option<String>,
    #[serde(default)] pub volume: Option<f32>,
    #[serde(default)] pub pan: Option<f32>,
    #[serde(default)] pub mute: Option<bool>,
    #[serde(default)] pub solo: Option<bool>,
    #[serde(default)] pub singer_id: Option<String>,
    #[serde(default)] pub instrument: Option<String>,
    #[serde(default)] pub colour: Option<String>,
    #[serde(default)] pub voice: Option<serde_json::Value>,
    #[serde(default)] pub fx: Option<Vec<serde_json::Value>>,
    #[serde(default)] pub sends: Option<Vec<serde_json::Value>>,
    #[serde(default)] pub bus: Option<String>,
}

pub async fn patch_track(
    State(st): State<AppState>, Path(id): Path<String>, Json(req): Json<PatchTrackReq>,
) -> axum::response::Response {
    st.snapshot().await;
    let mut p = st.project.write().await;
    match p.track_mut(&id) {
        Some(t) => {
            let content_changed = req.singer_id.is_some() || req.instrument.is_some() || req.voice.is_some();
            if let Some(v) = req.name { t.name = v; }
            if let Some(v) = req.volume { t.volume = v.clamp(0.0, 1.0); }
            if let Some(v) = req.pan { t.pan = v.clamp(-1.0, 1.0); }
            if let Some(v) = req.mute { t.mute = v; }
            if let Some(v) = req.solo { t.solo = v; }
            if let Some(v) = req.singer_id { t.singer_id = v; }
            if let Some(v) = req.instrument { t.instrument = v; }
            if let Some(v) = req.colour { t.colour = v; }
            if let Some(v) = req.voice { t.voice = v; }
            if let Some(v) = req.fx { t.fx = v; }
            if let Some(v) = req.sends { t.sends = v; }
            if let Some(v) = req.bus { t.bus = v; }
            if content_changed { st.bump_rev(); } else { st.bump_mix_rev(); }
            drop(p);
            ok(json!({"ok": true})).into_response()
        }
        None => (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": "track"}))).into_response(),
    }
}

pub async fn del_track(State(st): State<AppState>, Path(id): Path<String>) -> axum::response::Response {
    st.snapshot().await;
    let mut p = st.project.write().await;
    p.tracks.retain(|t| t.id != id);
    st.bump_rev();
    drop(p);
    ok(json!({"ok": true}))
}

// ---------- 音符 ----------
#[derive(Deserialize)]
pub struct AddNoteReq {
    pub tick: u32,
    #[serde(default = "default_dur")] pub duration: u32,
    pub key: u8,
    #[serde(default)] pub lyric: String,
    #[serde(default = "one_f")] pub velocity: f32,
    #[serde(default)] pub legato: bool,
    #[serde(default = "default_kind")] pub kind: String,
    #[serde(default)] pub vibrato: Option<svs_core::phrase::Vibrato>,
    #[serde(default)] pub attack_ms: Option<f32>,
    #[serde(default)] pub decay_ms: Option<f32>,
}
fn default_dur() -> u32 { PPQ }
fn one_f() -> f32 { 1.0 }
fn default_kind() -> String { "sung".into() }

fn valid_expression(kind: &str, vibrato: Option<&svs_core::phrase::Vibrato>, attack: Option<f32>, decay: Option<f32>) -> bool {
    ["sung", "breath"].contains(&kind)
        && attack.map_or(true, |v| v.is_finite() && v >= 0.0)
        && decay.map_or(true, |v| v.is_finite() && v >= 0.0)
        && vibrato.map_or(true, |v| v.depth_cents.is_finite() && v.depth_cents >= 0.0
            && v.frequency_hz.is_finite() && v.frequency_hz > 0.0
            && v.delay_ms.is_finite() && v.delay_ms >= 0.0
            && v.onset_ms.is_finite() && v.onset_ms >= 0.0
            && v.release_ms.is_finite() && v.release_ms >= 0.0)
}
pub async fn add_note(
    State(st): State<AppState>, Path(id): Path<String>, Json(req): Json<AddNoteReq>,
) -> axum::response::Response {
    if req.duration == 0 || !req.velocity.is_finite() || !(0.0..=1.0).contains(&req.velocity)
        || !valid_expression(&req.kind, req.vibrato.as_ref(), req.attack_ms, req.decay_ms) {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error":"VALIDATION","detail":"invalid note or expression"}))).into_response();
    }
    st.snapshot().await;
    let mut p = st.project.write().await;
    match p.track_mut(&id) {
        Some(t) => {
            let n = Note {
                id: uuid::Uuid::new_v4().to_string(),
                tick: req.tick, duration: req.duration, key: req.key,
                velocity: req.velocity, lyric: req.lyric,
                phonemes: vec![], legato: req.legato,
                head_ticks: 0, tail_ticks: 0, kind: req.kind, vibrato: req.vibrato,
                attack_ms: req.attack_ms, decay_ms: req.decay_ms,
            };
            let nid = n.id.clone();
            t.notes.push(n);
            st.bump_rev();
            drop(p);
            ok(json!({"ok": true, "id": nid})).into_response()
        }
        None => (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": "track"}))).into_response(),
    }
}

#[derive(Deserialize)]
pub struct PatchNoteReq {
    #[serde(default)] pub tick: Option<u32>,
    #[serde(default)] pub duration: Option<u32>,
    #[serde(default)] pub key: Option<u8>,
    #[serde(default)] pub lyric: Option<String>,
    #[serde(default)] pub velocity: Option<f32>,
    #[serde(default)] pub legato: Option<bool>,
    #[serde(default)] pub phonemes: Option<Vec<String>>,
    #[serde(default)] pub kind: Option<String>,
    #[serde(default, deserialize_with = "crate::expression::nullable_patch")] pub vibrato: Option<Option<svs_core::phrase::Vibrato>>,
    #[serde(default, deserialize_with = "crate::expression::nullable_patch")] pub attack_ms: Option<Option<f32>>,
    #[serde(default, deserialize_with = "crate::expression::nullable_patch")] pub decay_ms: Option<Option<f32>>,
}
pub async fn patch_note(
    State(st): State<AppState>, Path((id, nid)): Path<(String, String)>, Json(req): Json<PatchNoteReq>,
) -> axum::response::Response {
    if req.duration == Some(0) || req.velocity.map_or(false, |v| !v.is_finite() || !(0.0..=1.0).contains(&v))
        || req.kind.as_ref().map_or(false, |k| !["sung", "breath"].contains(&k.as_str()))
        || req.attack_ms.flatten().map_or(false, |v| !v.is_finite() || v < 0.0)
        || req.decay_ms.flatten().map_or(false, |v| !v.is_finite() || v < 0.0)
        || req.vibrato.as_ref().and_then(Option::as_ref).map_or(false, |v| !valid_expression("sung", Some(v), None, None)) {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error":"VALIDATION","detail":"invalid note or expression"}))).into_response();
    }
    st.snapshot().await;
    let mut p = st.project.write().await;
    match p.track_mut(&id).and_then(|t| t.notes.iter_mut().find(|n| n.id == nid)) {
        Some(n) => {
            if let Some(v) = req.tick { n.tick = v; }
            if let Some(v) = req.duration { n.duration = v; }
            if let Some(v) = req.key { n.key = v; }
            if let Some(v) = req.lyric { n.lyric = v; }
            if let Some(v) = req.velocity { n.velocity = v; }
            if let Some(v) = req.legato { n.legato = v; }
            if let Some(v) = req.phonemes { n.phonemes = v; }
            if let Some(v) = req.kind { n.kind = v; }
            if let Some(v) = req.vibrato { n.vibrato = v; }
            if let Some(v) = req.attack_ms { n.attack_ms = v; }
            if let Some(v) = req.decay_ms { n.decay_ms = v; }
            st.bump_rev();
            drop(p);
            ok(json!({"ok": true})).into_response()
        }
        None => (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": "note"}))).into_response(),
    }
}

pub async fn del_note(
    State(st): State<AppState>, Path((id, nid)): Path<(String, String)>,
) -> axum::response::Response {
    st.snapshot().await;
    let mut p = st.project.write().await;
    if let Some(t) = p.track_mut(&id) {
        t.notes.retain(|n| n.id != nid);
    }
    st.bump_rev();
    drop(p);
    ok(json!({"ok": true}))
}

pub async fn list_notes(State(st): State<AppState>, Path(id): Path<String>) -> axum::response::Response {
    let p = st.project.read().await;
    match p.track(&id) {
        Some(t) => ok(serde_json::to_value(&t.notes).unwrap_or_else(|_| json!([]))),
        None => (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": "track"}))).into_response(),
    }
}

// ---------- 曲线 ----------
#[derive(Deserialize)]
pub struct PaintReq {
    pub param: String,
    pub from_tick: u32,
    pub to_tick: u32,
    /// null 擦除；缺省 layer 保持旧工程的 user 画笔行为。
    pub value: Option<f32>,
    #[serde(default = "default_curve_layer")]
    pub layer: String,
}

fn default_curve_layer() -> String { "user".into() }

pub async fn paint_curve(
    State(st): State<AppState>, Path(id): Path<String>, Json(req): Json<PaintReq>,
) -> axum::response::Response {
    if !svs_core::PARAMS.contains(&req.param.as_str())
        || !["user", "envelope", "direct", "global"].contains(&req.layer.as_str())
        || req.from_tick >= req.to_tick
        || req.to_tick > 10_000_000
        || req.value.is_some_and(|v| !v.is_finite()) {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "BAD_REQUEST", "detail": "param/layer/range/value"}))).into_response();
    }
    let (lo, hi) = svs_core::curve::param_range(&req.param);
    let lo = if req.layer == "direct" { lo } else { -hi };
    let v = req.value.map(|v| v.clamp(lo, hi)).unwrap_or(f32::NAN);
    st.snapshot().await;
    let mut p = st.project.write().await;
    match p.track_mut(&id) {
        Some(t) => {
            let to_tick = req.to_tick;
            let len = to_tick.max(t.notes.iter().map(|n| n.tick + n.duration).max().unwrap_or(0)) as usize + 1;
            let c = t.curves.channels.entry(req.param.clone()).or_default();
            let layer = c.editable_layer(&req.layer).expect("validated editable layer");
            let mut arr = ParamCurve::decode_f32(layer);
            if arr.len() < len {
                arr.resize(len, f32::NAN);
            }
            for i in req.from_tick.min(len as u32)..req.to_tick.min(len as u32) {
                arr[i as usize] = v;
            }
            *layer = ParamCurve::encode_f32(&arr);
            st.bump_rev();
            drop(p);
            ok(json!({"ok": true})).into_response()
        }
        None => (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": "track"}))).into_response(),
    }
}

pub async fn get_curve(
    State(st): State<AppState>, Path((id, param)): Path<(String, String)>,
    axum::extract::Query(q): axum::extract::Query<std::collections::HashMap<String, String>>,
) -> axum::response::Response {
    let len: usize = q.get("len").and_then(|s| s.parse().ok()).unwrap_or(1920);
    if !svs_core::PARAMS.contains(&param.as_str()) || len > 10_000_000 {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "BAD_REQUEST", "detail": "param/len"}))).into_response();
    }
    let p = st.project.read().await;
    let Some(track) = p.track(&id) else {
        return (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": "track"}))).into_response();
    };
    let empty = ParamCurve::default();
    let c = track.curves.channels.get(&param).unwrap_or(&empty);
    ok(json!({"param": param, "len": len,
        "base_b64": c.base, "user_b64": c.user, "envelope_b64": c.envelope,
        "direct_b64": c.direct, "global_b64": c.global,
        "effective_b64": ParamCurve::encode_f32(&ParamCurve::effective(&param, len)(c))
    }))
}

// ---------- 合成/走带/渲染 ----------
#[derive(Deserialize)]
pub struct PatchMasterReq {
    #[serde(default)] pub master_volume: Option<f32>,
    #[serde(default)] pub metronome: Option<bool>,
    #[serde(default)] pub metronome_level: Option<f32>,
}

pub async fn patch_master(State(st): State<AppState>, Json(req): Json<PatchMasterReq>) -> axum::response::Response {
    let mut p = st.project.write().await;
    if let Some(v) = req.master_volume { p.master_volume = v.clamp(0.0, 1.0); }
    if let Some(v) = req.metronome { p.metronome = v; }
    if let Some(v) = req.metronome_level { p.metronome_level = v.clamp(0.0, 1.0); }
    let mv = p.master_volume; let mt = p.metronome;
    st.bump_mix_rev();
    drop(p);
    ok(json!({"ok": true, "master_volume": mv, "metronome": mt}))
}

#[derive(Deserialize)]
pub struct SynthReq {
    #[serde(default)] pub track: Option<String>,
}

pub async fn synth(State(st): State<AppState>, req: Option<Json<SynthReq>>) -> axum::response::Response {
    let track = req.and_then(|Json(r)| r.track);
    match engine::synth_track(&st, track.as_deref()).await {
        Ok((done, total, hits)) => ok(json!({"ok": true, "phrases_done": done, "phrases_total": total, "cache_hits": hits})),
        Err(e) => (axum::http::StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error": "SYNTH_FAILED", "detail": e.to_string()}))).into_response(),
    }
}

pub async fn transport(
    State(st): State<AppState>, Path(action): Path<String>,
) -> axum::response::Response {
    let tempos = st.project.read().await.tempos.clone();
    let mut tp = st.transport.lock().await;
    match action.as_str() {
        "play" => {
            if tp.playing { return ok(json!({"ok": true, "playing": true})); }
            tp.playing = true;
            tp.start_ms = svs_core::timeline::tick_to_ms(&tempos, tp.start_tick);
            tp.started_at = Some(std::time::Instant::now());
            tp.seek_gen += 1;
            let gen = tp.seek_gen;
            let tick = tp.start_tick;
            drop(tp);
            st.broadcast(json!({"type": "transport.command", "action": "play", "tick": tick})).await;
            spawn_transport_loop(st, gen);
            ok(json!({"ok": true, "playing": true}))
        }
        "stop" => {
            if let Some(started) = tp.started_at.take() {
                tp.start_ms += started.elapsed().as_secs_f64() * 1000.0;
                tp.start_tick = svs_core::timeline::ms_to_tick(&tempos, tp.start_ms);
            }
            tp.playing = false;
            tp.seek_gen += 1;
            let tick = tp.start_tick;
            drop(tp);
            st.broadcast(json!({"type": "transport.command", "action": "stop", "tick": tick})).await;
            ok(json!({"ok": true, "playing": false, "tick": tick}))
        }
        _ => (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "BAD_REQUEST", "detail": "action"}))).into_response(),
    }
}

#[derive(Deserialize)]
pub struct SeekReq {
    #[serde(default)] pub tick: Option<u32>,
    #[serde(default)] pub ms: Option<f64>,
}

pub async fn transport_seek(State(st): State<AppState>, Json(req): Json<SeekReq>) -> axum::response::Response {
    let tempos = st.project.read().await.tempos.clone();
    let tick = match (req.tick, req.ms) {
        (Some(tick), None) => tick,
        (None, Some(ms)) if ms.is_finite() && ms >= 0.0 => svs_core::timeline::ms_to_tick(&tempos, ms),
        _ => return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "BAD_REQUEST", "detail": "provide tick or nonnegative ms"}))).into_response(),
    };
    let gen = {
        let mut tp = st.transport.lock().await;
        tp.start_tick = tick;
        tp.start_ms = svs_core::timeline::tick_to_ms(&tempos, tick);
        tp.started_at = Some(std::time::Instant::now());
        tp.playing = true;
        tp.seek_gen += 1;
        tp.seek_gen
    };
    st.broadcast(json!({"type": "transport.command", "action": "seek", "tick": tick})).await;
    spawn_transport_loop(st, gen);
    ok(json!({"ok": true, "tick": tick}))
}

/// 远程走带按真实经过时间推进；浏览器本地试听以声卡时钟为准。
fn spawn_transport_loop(st: AppState, my_gen: u64) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(std::time::Duration::from_millis(40)).await;
            let (tempos, end_tick) = {
                let p = st.project.read().await;
                (p.tempos.clone(), p.end_tick() + PPQ)
            };
            let end_ms = svs_core::timeline::tick_to_ms(&tempos, end_tick);
            let mut tp = st.transport.lock().await;
            if !tp.playing || tp.seek_gen != my_gen { break; }
            let ms = (tp.start_ms + tp.started_at.map(|t| t.elapsed().as_secs_f64() * 1000.0).unwrap_or(0.0)).min(end_ms);
            let tick = svs_core::timeline::ms_to_tick(&tempos, ms);
            tp.start_tick = tick;
            let playing = ms < end_ms;
            if !playing { tp.playing = false; tp.started_at = None; tp.start_ms = ms; }
            drop(tp);
            st.broadcast(json!({"type": "transport.time", "playing": playing, "tick": tick, "ms": ms})).await;
            if !playing { break; }
        }
    });
}

#[derive(Deserialize, Default)]
pub struct RenderReq {
    #[serde(default)] pub path: String,
    #[serde(default)] pub from_ms: Option<f64>,
    #[serde(default)] pub to_ms: Option<f64>,
}

pub async fn render_wav(State(st): State<AppState>, req: Option<Json<RenderReq>>) -> axum::response::Response {
    let _gate = match st.render_gate.try_lock() {
        Ok(g) => g,
        Err(_) => return (axum::http::StatusCode::CONFLICT, Json(json!({"error":"RENDER_BUSY"}))).into_response(),
    };
    if let Err(e) = engine::ensure_audio_service(&st).await {
        return (axum::http::StatusCode::SERVICE_UNAVAILABLE, Json(json!({"error":"SYNTH_FAILED","detail":e.to_string()}))).into_response();
    }
    // Snapshot project and revision together before synthesis awaits.
    let (project, render_rev) = {
        let p = st.project.read().await;
        (p.clone(), st.edit_rev.load(std::sync::atomic::Ordering::SeqCst))
    };
    let mut mix = match crate::rendering::mix_project(&st, &project).await {
        Ok(pcm) => pcm,
        Err(e) => return (axum::http::StatusCode::BAD_GATEWAY, Json(json!({"error":"MIX_FAILED","detail":e.to_string()}))).into_response(),
    };
    let request = req.map(|Json(r)| r).unwrap_or_default();
    let duration_ms = mix.len() as f64 / 2. / SAMPLE_RATE as f64 * 1000.;
    let from = request.from_ms.unwrap_or(0.);
    let to = request.to_ms.unwrap_or(duration_ms);
    if !from.is_finite() || !to.is_finite() || from < 0. || to <= from || to > duration_ms {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error":"BAD_RANGE","detail":"require 0 <= from_ms < to_ms <= rendered duration"}))).into_response();
    }
    let start = (from * SAMPLE_RATE as f64 / 1000.).round() as usize * 2;
    let end = ((to * SAMPLE_RATE as f64 / 1000.).round() as usize * 2).min(mix.len());
    mix = mix[start..end].to_vec();
    let path = if request.path.is_empty() { "output.wav".to_string() } else { request.path };
    if render_rev != st.edit_rev.load(std::sync::atomic::Ordering::SeqCst) {
        return (axum::http::StatusCode::CONFLICT, Json(json!({"error":"STALE_MIX","detail":"project edited during render"}))).into_response();
    }
    let spec = hound::WavSpec {
        channels: 2, sample_rate: SAMPLE_RATE, bits_per_sample: 32,
        sample_format: hound::SampleFormat::Float,
    };
    match hound::WavWriter::create(&path, spec) {
        Ok(mut w) => {
            for s in &mix {
                let _ = w.write_sample(*s);
            }
            if let Err(e) = w.finalize() {
                return (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "INTERNAL", "detail": e.to_string()}))).into_response();
            }
            let samples = mix.len() / 2;
            *st.last_mix.write().await = Some(mix);
            *st.mix_rev.write().await = render_rev;
            ok(json!({"ok": true, "path": path, "samples": samples, "frames": samples, "channels": 2, "sample_rate": SAMPLE_RATE}))
        }
        Err(e) => (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "INTERNAL", "detail": e.to_string()}))).into_response(),
    }
}

/// 最近一次渲染的 PCM 原始字节（f32le），供浏览器 WebAudio 播放。
pub async fn get_pcm(State(st): State<AppState>) -> axum::response::Response {
    // 混音过期（渲染后工程又被编辑）→ 通知客户端重渲
    if !st.mix_fresh().await {
        return (
            axum::http::StatusCode::CONFLICT,
            Json(json!({"error": "STALE_MIX", "detail": "project edited since last render"})),
        )
            .into_response();
    }
    let m = st.last_mix.read().await;
    match m.as_ref() {
        Some(mix) => {
            let mut bytes = Vec::with_capacity(mix.len() * 4);
            for v in mix {
                bytes.extend_from_slice(&v.to_le_bytes());
            }
            axum::response::Response::builder()
                .status(axum::http::StatusCode::OK)
                .header("Content-Type", "application/octet-stream")
                .header("X-Channels", "2")
                .header("X-Sample-Rate", SAMPLE_RATE.to_string())
                .body(axum::body::Body::from(bytes))
                .unwrap()
        }
        None => (axum::http::StatusCode::NOT_FOUND, Json(json!({"error": "NOT_FOUND", "detail": "no render yet"}))).into_response(),
    }
}



pub async fn health(State(st): State<AppState>) -> axum::response::Response {
    let inf = st.inference_url.read().await.is_some();
    let p = st.project.read().await;
    ok(json!({
        "ok": true, "server": true, "inference": inf,
        "tracks": p.tracks.len(), "name": p.name,
    }))
}

// ---------- WS ----------
pub async fn ws_handler(State(st): State<AppState>, ws: WebSocketUpgrade) -> axum::response::Response {
    ws.on_upgrade(|socket| ws_loop(socket, st))
}

async fn ws_loop(mut socket: WebSocket, st: AppState) {
    let mut rx = st.ws_tx.subscribe();
    let (mut tx, mut rrx) = socket.split();
    use futures_util::{SinkExt, StreamExt};
    let send_task = tokio::spawn(async move {
        while let Ok(msg) = rx.recv().await {
            if tx.send(Message::Text(msg.into())).await.is_err() {
                break;
            }
        }
    });
    // 接收方向：忽略 client 消息（保持连接）
    while let Some(Ok(_)) = rrx.next().await {}
    send_task.abort();
}

// ---------- ABC 曲谱导入（复用 abc2diffsinger 插件解析管线） ----------

#[derive(Deserialize, serde::Serialize)]
pub struct AbcImportReq {
    /// ABC 谱面全文（前端读文件后传文本，避免服务端依赖文件路径）
    pub abc: String,
    /// 多声部时必填（如 "Vocal"）；缺省自动取第一个声部
    #[serde(default)]
    pub voice: Option<String>,
    /// 演唱歌词（留空 = 全程「啊」占位）
    #[serde(default)]
    pub lyrics: Option<String>,
    /// 整体升降半音
    #[serde(default)]
    pub key_shift: Option<i32>,
    /// 轨道名（默认 "ABC 主唱"）
    #[serde(default)]
    pub track_name: Option<String>,
    /// 自动编曲完整乐队（和声/钢琴/贝斯/鼓/和弦轨），默认 true
    #[serde(default)]
    pub full_band: Option<bool>,
}

/// POST /api/import/abc：解析 ABC → 生成 sing 轨（含 BPM/拍号），返回轨 id 与统计。
pub async fn import_abc(State(st): State<AppState>, Json(req): Json<AbcImportReq>) -> axum::response::Response {
    st.snapshot().await;
    // 调 Python 桥（复用 abc2diffsinger 的 parse_abc/inject_lyrics/split_lyrics）
    let script_dir = format!("{}/../../scripts", env!("CARGO_MANIFEST_DIR"));
    let py = if cfg!(windows) { "python" } else { "python3" };
    let body = match serde_json::to_vec(&req) {
        Ok(b) => b,
        Err(e) => return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "BAD_REQUEST", "detail": e.to_string()}))).into_response(),
    };
    let out = tokio::task::spawn_blocking(move || {
        std::process::Command::new(py)
            .arg("abc_import.py")
            .current_dir(&script_dir)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .and_then(|mut child| {
                use std::io::Write;
                child.stdin.as_mut().unwrap().write_all(&body)?;
                let o = child.wait_with_output()?;
                Ok(o)
            })
    }).await;
    let output = match out {
        Ok(Ok(o)) => o,
        _ => return (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "INTERNAL", "detail": "failed to run abc_import.py"}))).into_response(),
    };
    if !output.status.success() {
        let msg = String::from_utf8_lossy(&output.stderr);
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "ABC_PARSE", "detail": msg.trim().to_string()}))).into_response();
    }
    let parsed: serde_json::Value = match serde_json::from_slice(&output.stdout) {
        Ok(v) => v,
        Err(e) => return (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "INTERNAL", "detail": format!("bridge output: {e}")}))).into_response(),
    };

    // 桥输出 → svs 音符
    let notes_json = parsed["notes"].as_array().cloned().unwrap_or_default();
    if notes_json.is_empty() {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "ABC_EMPTY", "detail": "解析不到任何音符"}))).into_response();
    }
    let notes: Vec<Note> = notes_json.iter().map(|n| Note {
        id: uuid::Uuid::new_v4().to_string(),
        tick: n["tick"].as_u64().unwrap_or(0) as u32,
        duration: n["duration"].as_u64().unwrap_or(PPQ as u64) as u32,
        key: n["key"].as_u64().unwrap_or(60) as u8,
        velocity: n["velocity"].as_f64().unwrap_or(1.0) as f32,
        lyric: n["lyric"].as_str().unwrap_or("a").to_string(),
        phonemes: vec![],
        legato: n["legato"].as_bool().unwrap_or(false),
        head_ticks: 0,
        tail_ticks: 0,
        kind: "sung".into(), vibrato: None, attack_ms: None, decay_ms: None,
    }).collect();
    let note_count = notes.len();

    // BPM / 拍号写入工程：工程仍是「默认单点」时直接采用 ABC 的值，否则保留现有曲速
    let bpm = parsed["bpm"].as_f64().unwrap_or(120.0);
    let beats = parsed["beats"].as_u64().unwrap_or(4) as u32;
    let beat_type = parsed["beat_type"].as_u64().unwrap_or(4) as u32;
    // 自动编曲：主旋律 + 和弦标记 → 和声/钢琴/贝斯/鼓/和弦轨（full_band=true 默认）
    let full_band = req.full_band.unwrap_or(true);
    let chords_json = parsed["chords"].as_array().cloned().unwrap_or_default();
    let chords: Vec<(u32, String)> = chords_json
        .iter()
        .filter_map(|c| Some((c["tick"].as_u64()? as u32, c["name"].as_str()?.to_string())))
        .collect();
    let extra_tracks: Vec<Track> = if full_band {
        let total = (parsed["total_ticks"].as_u64().unwrap_or(0) as u32)
            .max(notes.iter().map(|n| n.tick + n.duration).max().unwrap_or(0));
        let bar_ticks = beats * (PPQ * 4) / beat_type.max(1);
        svs_core::arrange::arrange(&notes, &chords, total, bar_ticks)
    } else {
        vec![]
    };
    let extra_count = extra_tracks.len();
    // 建 sing 轨（默认真 DiffSinger 音源「绮萱」，人声真唱而非合成器近似）
    let name = req.track_name.clone().unwrap_or_else(|| "ABC 主唱".into());
    let mut track = Track::new(TrackKind::Sing, &name);
    track.singer_id = "diffsinger.qixuan".into();
    track.notes = notes;
    let track_id = track.id.clone();
    let voice_used = parsed["voice"].as_str().unwrap_or("").to_string();
    {
        let mut p = st.project.write().await;
        let is_default = |tick: u32, val: f64| tick == 0 && (val - 120.0).abs() < 1e-6;
        if p.tempos.len() == 1 && is_default(p.tempos[0].tick, p.tempos[0].bpm) {
            p.tempos = vec![svs_core::timeline::TempoPoint { tick: 0, bpm, curve: svs_core::timeline::TempoCurve::Step }];
        }
        if p.time_signatures.len() == 1 && p.time_signatures[0].tick == 0
            && p.time_signatures[0].numerator == 4 && p.time_signatures[0].denominator == 4 {
            p.time_signatures = vec![svs_core::timeline::TimeSigPoint { tick: 0, numerator: beats, denominator: beat_type }];
        }
        p.tracks.push(track);
        p.tracks.extend(extra_tracks);
        st.bump_rev();
    }
    ok(json!({
        "ok": true, "track_id": track_id, "notes": note_count,
        "extra_tracks": extra_count,
        "bpm": bpm, "beats": beats, "beat_type": beat_type,
        "voice": voice_used,
        "missing_lyric": parsed["missing_lyric"].as_u64().unwrap_or(0),
    }))
}

// ---------- AI 歌词（复用 abc2diffsinger 插件 LLM 管线） ----------

#[derive(Deserialize, serde::Serialize)]
pub struct AiLyricsReq {
    pub theme: String,
    pub n_notes: u32,
    #[serde(default)]
    pub abc: Option<String>,
    #[serde(default)]
    pub voice: Option<String>,
}

/// POST /api/ai/lyrics：主题 → 恰好 n_notes 字的歌词（字数不符自动重试）。
pub async fn ai_lyrics(State(st): State<AppState>, Json(req): Json<AiLyricsReq>) -> axum::response::Response {
    let _ = st;
    let script_dir = format!("{}/../../scripts", env!("CARGO_MANIFEST_DIR"));
    let py = if cfg!(windows) { "python" } else { "python3" };
    let mut body = serde_json::to_value(&req).unwrap_or_else(|_| json!({}));
    body["mode"] = json!("ai_lyrics");
    let out = tokio::task::spawn_blocking(move || {
        std::process::Command::new(py)
            .arg("abc_import.py")
            .current_dir(&script_dir)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .and_then(|mut child| {
                use std::io::Write;
                child.stdin.as_mut().unwrap().write_all(body.to_string().as_bytes())?;
                child.wait_with_output()
            })
    }).await;
    let output = match out {
        Ok(Ok(o)) => o,
        _ => return (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "INTERNAL", "detail": "failed to run abc_import.py"}))).into_response(),
    };
    if !output.status.success() {
        let msg = String::from_utf8_lossy(&output.stderr);
        let parsed: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap_or_else(|_| json!({}));
        let detail = parsed["error"].as_str().map(|s| s.to_string())
            .unwrap_or_else(|| msg.lines().last().unwrap_or("LLM 调用失败").to_string());
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error": "AI_LYRICS", "detail": detail}))).into_response();
    }
    match serde_json::from_slice::<serde_json::Value>(&output.stdout) {
        Ok(v) => ok(v),
        Err(e) => (axum::http::StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": "INTERNAL", "detail": e.to_string()}))).into_response(),
    }
}

#[cfg(test)]
mod curve_tests {
    use super::*;

    #[tokio::test]
    async fn erasing_direct_restores_lower_layers_and_keeps_global() {
        let st = AppState::new();
        let mut track = Track::new(TrackKind::Sing, "Layers");
        let id = track.id.clone();
        track.curves.channels.insert("energy".into(), ParamCurve {
            base: ParamCurve::encode_f32(&[0.4, 0.4]),
            user: ParamCurve::encode_f32(&[0.2, 0.2]),
            global: ParamCurve::encode_f32(&[0.1, 0.1]),
            ..Default::default()
        });
        st.project.write().await.tracks.push(track);
        let paint = |layer: &str, value| PaintReq {
            param: "energy".into(), layer: layer.into(), from_tick: 0, to_tick: 1, value,
        };
        let response = paint_curve(State(st.clone()), Path(id.clone()), Json(paint("direct", Some(0.1)))).await;
        assert_eq!(response.status(), axum::http::StatusCode::OK);
        {
            let p = st.project.read().await;
            let c = &p.track(&id).unwrap().curves.channels["energy"];
            let effective = ParamCurve::effective("energy", 2)(c);
            assert!((effective[0] - 0.2).abs() < 1e-6);
            assert!((effective[1] - 0.7).abs() < 1e-6);
        }
        let response = paint_curve(State(st.clone()), Path(id.clone()), Json(paint("direct", None))).await;
        assert_eq!(response.status(), axum::http::StatusCode::OK);
        let p = st.project.read().await;
        let c = &p.track(&id).unwrap().curves.channels["energy"];
        assert!(ParamCurve::decode_f32(&c.direct)[0].is_nan());
        assert!((ParamCurve::effective("energy", 1)(c)[0] - 0.7).abs() < 1e-6);
        drop(p);
        let response = paint_curve(State(st.clone()), Path(id), Json(paint("base", Some(0.9)))).await;
        assert_eq!(response.status(), axum::http::StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn mixer_changes_keep_phrase_plan_valid_but_notes_invalidate() {
        use std::sync::atomic::Ordering;
        let st = AppState::new();
        let track = Track::new(TrackKind::Sing, "voice");
        let id = track.id.clone();
        st.project.write().await.tracks.push(track);
        let patch: PatchTrackReq = serde_json::from_value(json!({"volume": 0.3, "pan": -0.4, "mute": true})).unwrap();
        let response = patch_track(State(st.clone()), Path(id.clone()), Json(patch)).await;
        assert_eq!(response.status(), axum::http::StatusCode::OK);
        assert_eq!(st.audio_rev.load(Ordering::SeqCst), 0);
        assert_eq!(st.edit_rev.load(Ordering::SeqCst), 1);
        let patch: PatchTrackReq = serde_json::from_value(json!({"singer_id": "diffsinger.qixuan"})).unwrap();
        patch_track(State(st.clone()), Path(id), Json(patch)).await;
        assert_eq!(st.audio_rev.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn remote_seek_ms_uses_piecewise_tempo_and_stop_cancels_loop() {
        let st = AppState::new();
        st.project.write().await.tempos.push(svs_core::timeline::TempoPoint { tick: 960, bpm: 60.0, curve: svs_core::timeline::TempoCurve::Step });
        let mut messages = st.ws_tx.subscribe();
        transport_seek(State(st.clone()), Json(SeekReq { tick: None, ms: Some(2000.0) })).await;
        let command: serde_json::Value = serde_json::from_str(&messages.recv().await.unwrap()).unwrap();
        assert_eq!(command["action"], "seek");
        assert_eq!(command["tick"], 1440);
        transport(State(st.clone()), Path("stop".into())).await;
        assert!(!st.transport.lock().await.playing);
        let command: serde_json::Value = serde_json::from_str(&messages.recv().await.unwrap()).unwrap();
        assert_eq!(command["action"], "stop");
    }
}
