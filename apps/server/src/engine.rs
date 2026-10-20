//! 推理服务进程管理 + 短语合成调度。

use crate::state::AppState;
use svs_core::phrase::{phrase_hash, split_phrases, Note};
use svs_core::SAMPLE_RATE;
use serde_json::json;

pub const INFERENCE_PORT: u16 = 8760;

/// Start the inference worker from either the packaged runtime or the source tree.
pub async fn ensure_inference(st: AppState) {
    if st.inference_url.read().await.is_some() { return; }
    let local = format!("http://127.0.0.1:{INFERENCE_PORT}");
    if st.http.get(format!("{local}/health")).send().await.map(|r| r.status().is_success()).unwrap_or(false) {
        *st.inference_url.write().await = Some(local);
        return;
    }

    let desktop = std::env::var_os("SVS_DESKTOP_MODE").is_some();
    let service_dir = std::env::var_os("SVS_INFERENCE_SERVICE_DIR")
        .map(std::path::PathBuf::from)
        .or_else(|| {
            let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
            let source = root.join("../../packages/inference-service");
            if source.is_dir() { Some(source) } else { None }
        });
    let Some(service_dir) = service_dir.filter(|p| p.is_dir()) else {
        eprintln!("[engine] inference service directory missing; set SVS_INFERENCE_SERVICE_DIR");
        return;
    };

    let configured_python = std::env::var_os("SVS_GPU_PYTHON").map(std::path::PathBuf::from);
    if let Some(path) = configured_python.as_ref().filter(|path| !path.is_file()) {
        eprintln!("[engine] configured GPU Python is missing: {}", path.display());
        return;
    }
    let local_gpu_python = service_dir.join("../../../abc2diffsinger/runtime/venv/Scripts/python.exe");
    let configured_python = configured_python.or_else(|| {
        (!desktop && local_gpu_python.is_file()).then_some(local_gpu_python)
    });
    let packaged_python = if cfg!(windows) {
        service_dir.parent().unwrap_or(&service_dir).join("python").join("svs-python").join("svs-python.exe")
    } else {
        service_dir.parent().unwrap_or(&service_dir).join("python").join("svs-python").join("svs-python")
    };
    let packaged = desktop && configured_python.is_none();
    let (python, gpu) = if let Some(path) = configured_python.filter(|p| p.is_file()) {
        (path, true)
    } else if packaged_python.is_file() {
        (packaged_python, false)
    } else if desktop {
        eprintln!("[engine] packaged Python runtime missing; set SVS_GPU_PYTHON to a compatible Python environment");
        return;
    } else {
        let fallback = if cfg!(windows) { "python" } else { "python3" };
        (std::path::PathBuf::from(fallback), false)
    };

    let model_dir = std::env::var_os("SVS_MODEL_DIR").map(std::path::PathBuf::from)
        .or_else(|| {
            let bundled = service_dir.join("models");
            bundled.is_dir().then_some(bundled)
        });
    let mut command = std::process::Command::new(&python);
    #[cfg(windows)]
    if desktop {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW for desktop inference workers.
    }
    if !packaged {
        command.args(["-m", "inference_service.service_runner"]);
    }
    command.args(["--port", &INFERENCE_PORT.to_string()])
        .current_dir(&service_dir)
        .env("PYTHONPATH", service_dir.as_os_str())
        .env("PYTHONUTF8", "1")
        .env("SVS_INFERENCE_GPU", if gpu { "1" } else { "0" });
    if gpu {
        command.env("SVS_INFERENCE_DEVICE", std::env::var("SVS_INFERENCE_DEVICE").unwrap_or_else(|_| "cuda".into()));
        command.env("SVS_MUSIC_AI_PYTHON", std::env::var_os("SVS_MUSIC_AI_PYTHON").unwrap_or_else(|| python.clone().into_os_string()));
    }
    if let Some(model_dir) = model_dir { command.env("SVS_MODEL_DIR", model_dir); }
    if let Some(voicebank) = std::env::var_os("SVS_VOICEBANK") { command.env("SVS_VOICEBANK", voicebank); }
    eprintln!("[engine] inference python: {} (gpu={gpu})", python.display());
    match command.stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).spawn() {
        Ok(_) => println!("[engine] inference service spawning on :{INFERENCE_PORT}"),
        Err(e) => { eprintln!("[engine] failed to spawn inference: {e}"); return; }
    }

    for _ in 0..30 {
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        if let Ok(resp) = st.http.get(format!("http://127.0.0.1:{INFERENCE_PORT}/health")).send().await {
            if resp.status().is_success() {
                *st.inference_url.write().await = Some(local);
                println!("[engine] inference ready");
                return;
            }
        }
    }
    eprintln!("[engine] inference service not ready (synth will 503)");
}

/// G2P：中文/拉丁音素推断（轻量内联版；与 Python 词典同步的常用映射子集）。
/// 完整多语言 G2P 由 g2p-engine 提供；这里覆盖字典缺失场景的退化输入。
pub(crate) fn g2p_inline(lyric: &str) -> Vec<String> {
    let zh: &[(&str, &str)] = &[
        ("你", "n i3"), ("好", "h ao3"), ("世", "sh i4"), ("界", "j ie4"),
        ("我", "w o3"), ("爱", "ai4"), ("唱", "ch ang4"), ("歌", "g e1"),
        ("的", "d i4"), ("了", "le5"), ("是", "sh i4"), ("在", "z ai4"),
        ("一", "y i2"), ("啊", "a5"), ("哦", "o2"), ("嗯", "en3"),
    ];
    for (ch, phs) in zh {
        if lyric == *ch {
            return phs.split(' ').map(String::from).collect();
        }
    }
    if lyric.is_empty() {
        return vec!["ah".into()];
    }
    vec![lyric.chars().next().map(|_| "ah".into()).unwrap_or_else(|| "ah".into())]
}

/// Immutable request describing one synthesis unit; shared by export and live playback.
pub struct PhraseSpec {
    pub endpoint: &'static str,
    pub payload: serde_json::Value,
    pub offset_tick: u32,
}

/// Build one canonical phrase payload so export and audition share hashes and DSP inputs.
pub fn phrase_spec(
    track: &svs_core::project::Track,
    notes: &[&Note],
    tempos: &[svs_core::timeline::TempoPoint],
    native: bool,
    instrument_timbre: Option<&str>,
) -> PhraseSpec {
    let native = native || is_native_id(&track.singer_id);
    let singer = instrument_timbre.map(|t| format!("inst.{t}")).unwrap_or_else(|| track.singer_id.clone());
    let offset = notes.iter().map(|n| n.tick).min().unwrap_or(0);
    let end = notes.iter().map(|n| n.tick.saturating_add(n.duration)).max().unwrap_or(offset);
    let curve_key = phrase_cache_key(track, tempos, offset, end, notes);
    let tag = if native { "diffsinger-native-controls-5" } else { "dsp-layers-2" };
    let hash = phrase_hash(&singer, notes, &format!("{}:{}", curve_key, track.voice), SAMPLE_RATE, tag);
    let note_json: Vec<serde_json::Value> = notes.iter().map(|n| {
        let phonemes = if instrument_timbre.is_some() || (native && n.phonemes.is_empty()) { Vec::new() }
            else if n.phonemes.is_empty() { g2p_inline(&n.lyric) } else { n.phonemes.clone() };
        json!({
            "tick": n.tick - offset, "duration": n.duration, "key": n.key,
            "velocity": n.velocity, "lyric": if instrument_timbre.is_some() { "" } else { &n.lyric },
            "phonemes": phonemes, "legato": n.legato, "kind": n.kind,
            "vibrato": n.vibrato, "attack_ms": n.attack_ms, "decay_ms": n.decay_ms,
        })
    }).collect();
    let mut payload = json!({
        "hash": hash, "notes": note_json, "offset_ticks": offset,
        "tempos": tempos, "singer": if instrument_timbre.is_some() { "" } else { &track.singer_id },
        "curves": track.curves, "voice": track.voice,
    });
    if let Some(timbre) = instrument_timbre { payload["timbre"] = json!(timbre); }
    if native { payload["key_shift"] = json!(0); }
    PhraseSpec { endpoint: if native { "render_ds" } else { "synth_phrase" }, payload, offset_tick: offset }
}

/// Read and validate audio from inference using a caller-held render gate.
pub async fn fetch_audio(st: &AppState, spec: &serde_json::Value, endpoint: &str, timeout: u64) -> anyhow::Result<Vec<f32>> {
    let base = st.inference_url.read().await.clone().ok_or_else(|| anyhow::anyhow!("INFERENCE_DOWN"))?;
    let resp = st.http.post(format!("{base}/{endpoint}")).json(spec)
        .timeout(std::time::Duration::from_secs(timeout)).send().await?;
    if !resp.status().is_success() { return Err(anyhow::anyhow!("{}", resp.text().await?)); }
    let body: serde_json::Value = resp.json().await?;
    let audio = body["audio"].as_str().ok_or_else(|| anyhow::anyhow!("missing audio"))?;
    use base64::Engine;
    let bytes = base64::engine::general_purpose::STANDARD.decode(audio)?;
    anyhow::ensure!(bytes.len() % 4 == 0, "unaligned f32 PCM");
    let pcm: Vec<f32> = bytes.chunks_exact(4).map(|c| f32::from_le_bytes([c[0],c[1],c[2],c[3]])).collect();
    anyhow::ensure!(pcm.iter().all(|v| v.is_finite()), "non-finite PCM");
    Ok(pcm)
}

/// Ensure an already-running service is reused; only spawn when health probe fails.
pub async fn ensure_audio_service(st: &AppState) -> anyhow::Result<()> {
    let healthy = match st.inference_url.read().await.clone() {
        Some(url) => st.http.get(format!("{url}/health")).send().await.map(|r| r.status().is_success()).unwrap_or(false),
        None => false,
    };
    if !healthy {
        *st.inference_url.write().await = None;
        ensure_inference(st.clone()).await;
    }
    if st.inference_url.read().await.is_none() { anyhow::bail!("INFERENCE_DOWN"); }
    Ok(())
}

pub async fn synth_track(st: &AppState, track_id: Option<&str>) -> anyhow::Result<(usize, usize, usize)> {
    let _gate = st.render_gate.lock().await;
    ensure_audio_service(st).await?;
    let (tracks, tempos) = {
        let project = st.project.read().await;
        (project.tracks.iter().filter(|t| t.kind == svs_core::project::TrackKind::Sing)
            .filter(|t| track_id.map_or(true, |id| t.id == id || t.name == id)).cloned().collect::<Vec<_>>(), project.tempos.clone())
    };
    let mut done = 0usize; let mut total = 0usize; let mut hits = 0usize;
    for track in tracks {
        let native = is_native_id(&track.singer_id);
        let phrases = phrase_groups(&track);
        total += phrases.len();
        for ph in phrases {
            let built = phrase_spec(&track, &ph, &tempos, native, None);
            let resp = st.http.post(format!("{}/{}", st.inference_url.read().await.as_ref().unwrap(), built.endpoint))
                .json(&built.payload).timeout(std::time::Duration::from_secs(if native {1800} else {60})).send().await?;
            if !resp.status().is_success() { anyhow::bail!("synthesis failed: {}", resp.text().await?); }
            let body: serde_json::Value = resp.json().await?;
            if body["cache"].as_str() == Some("hit") { hits += 1; }
            done += 1;
            st.broadcast(json!({"type":"engine.status","synth_state":"rendering","phrases_done":done,"phrases_total":total})).await;
        }
    }
    Ok((done,total,hits))
}

/// Whether a singer ID selects the native model-backed rendering path.
pub fn is_native_id(singer_id: &str) -> bool {
    singer_id.starts_with("diffsinger") || singer_id.starts_with("voice.")
}
/// Split each track identically for native and DSP backends.
pub fn phrase_groups(track: &svs_core::project::Track) -> Vec<Vec<&Note>> {
    split_phrases(&track.notes)
}

/// Cache key varies only with curve samples and tempo segments used by this phrase.
pub fn phrase_cache_key(track: &svs_core::project::Track, tempos: &[svs_core::timeline::TempoPoint], start: u32, end: u32, notes: &[&Note]) -> String {
    const GAP_BITS: u32 = 0x7fc0_0000;
    let curves: Vec<_> = svs_core::PARAMS.iter().map(|param| {
        let c = track.curves.channels.get(*param);
        let layers: Vec<Vec<u32>> = ["base", "user", "envelope", "direct", "global"].iter().map(|layer| {
            let Some(data) = c.and_then(|c| c.layer(layer)) else { return Vec::new(); };
            let values = svs_core::curve::ParamCurve::decode_f32(data);
            let mut window: Vec<u32> = (start as usize..end as usize).map(|i| {
                values.get(i).filter(|v| v.is_finite()).map(|v| v.to_bits()).unwrap_or(GAP_BITS)
            }).collect();
            while window.last() == Some(&GAP_BITS) { window.pop(); }
            window
        }).collect();
        (*param, layers)
    }).collect();
    let mut relevant: Vec<_> = tempos.iter().filter(|t| t.tick <= start).last().cloned().into_iter().collect();
    relevant.extend(tempos.iter().filter(|t| t.tick > start && t.tick <= end).cloned());
    if relevant.last().is_some_and(|t| t.curve == svs_core::timeline::TempoCurve::Linear) {
        if let Some(next) = tempos.iter().find(|t| t.tick > end) { relevant.push(next.clone()); }
    }
    let controls: Vec<_> = notes.iter().map(|n| (&n.kind, &n.vibrato, n.attack_ms, n.decay_ms)).collect();
    serde_json::to_string(&(start, end, curves, relevant, controls)).expect("phrase cache key serializes")
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phrase_cache_reuses_omitted_and_outside_only_curve_data() {
        let mut track = svs_core::project::Track::new(svs_core::project::TrackKind::Sing, "voice");
        let note = note(480, 240);
        let refs = [&note];
        let mut tempos = vec![svs_core::timeline::TempoPoint { tick: 0, bpm: 120.0, curve: svs_core::timeline::TempoCurve::Step }];
        let first = phrase_cache_key(&track, &tempos, 480, 720, &refs);
        let mut values = vec![f32::NAN; 960];
        values[800] = 1.0;
        track.curves.channels.entry("pitch".into()).or_default().user = svs_core::curve::ParamCurve::encode_f32(&values);
        assert_eq!(first, phrase_cache_key(&track, &tempos, 480, 720, &refs));
        tempos.push(svs_core::timeline::TempoPoint { tick: 1920, bpm: 60.0, curve: svs_core::timeline::TempoCurve::Step });
        assert_eq!(first, phrase_cache_key(&track, &tempos, 480, 720, &refs));
        values[500] = 1.0;
        track.curves.channels.get_mut("pitch").unwrap().user = svs_core::curve::ParamCurve::encode_f32(&values);
        assert_ne!(first, phrase_cache_key(&track, &tempos, 480, 720, &refs));
    }

    fn note(tick: u32, duration: u32) -> Note {
        Note { id: format!("{tick}"), tick, duration, key: 60, velocity: 1.0, lyric: "la".into(), phonemes: vec![], legato: false, head_ticks: 0, tail_ticks: 0, kind: "sung".into(), vibrato: None, attack_ms: None, decay_ms: None }
    }

    #[test]
    fn phrase_payload_preserves_source_offset_and_native_whole_track_contract() {
        let mut track = svs_core::project::Track::new(svs_core::project::TrackKind::Sing, "voice");
        track.singer_id = "singer-a".into();
        let note = note(480, 240);
        let tempos = vec![svs_core::timeline::TempoPoint { tick: 0, bpm: 120.0, curve: svs_core::timeline::TempoCurve::Step }];
        let unit = phrase_spec(&track, &[&note], &tempos, false, None);
        assert_eq!(unit.offset_tick, 480);
        assert_eq!(unit.payload["notes"][0]["tick"], 0);
        let native = phrase_spec(&track, &[&note], &tempos, true, None);
        assert_eq!(native.offset_tick, 480);
        assert_eq!(native.payload["notes"][0]["tick"], 0);
    }
    #[test]
    fn native_empty_phonemes_stay_empty_for_contextual_phonemizer() {
        let track = svs_core::project::Track::new(svs_core::project::TrackKind::Sing, "voice");
        let note = note(0, 240);
        let tempos = vec![svs_core::timeline::TempoPoint { tick: 0, bpm: 120.0, curve: svs_core::timeline::TempoCurve::Step }];
        let native = phrase_spec(&track, &[&note], &tempos, true, None);
        assert_eq!(native.payload["notes"][0]["phonemes"], json!([]));
        let dsp = phrase_spec(&track, &[&note], &tempos, false, None);
        assert_eq!(dsp.payload["notes"][0]["phonemes"], json!(["ah"]));
    }

}
