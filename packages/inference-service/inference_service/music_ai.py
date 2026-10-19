"""Optional music-AI endpoints with explicit local-model requirements.

No model weights are downloaded by this module. Install the Python packages and
place licensed pretrained weights in the locations documented by /music-ai/status.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import tempfile
from pathlib import Path
import subprocess
import sys
from flask import jsonify, request

from .config import ABC2VOCALS_DIR

MAX_AUDIO_BYTES = 100 * 1024 * 1024
MAX_ABC_CHARS = 200_000
MAX_LLM_OUTPUT_CHARS = 100_000


def _abc_module():
    if not (ABC2VOCALS_DIR / "abc2vocals.py").is_file():
        raise RuntimeError(f"abc2vocals unavailable at configured plugin path: {ABC2VOCALS_DIR}")
    import sys
    path = str(ABC2VOCALS_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    import abc2vocals
    return abc2vocals


def _model_status():
    """Report package, checkpoint and CUDA readiness without downloading models."""
    worker_env = os.environ.get("SVS_MUSIC_AI_PYTHON")
    worker_python = Path(worker_env or sys.executable)
    worker_ready = worker_python.is_file() and (bool(worker_env) or not getattr(sys, "frozen", False))
    worker_model_ready = False
    worker_device = None
    if worker_ready:
        probe = "import importlib.util,json,pathlib,onnxruntime as ort,torch; s=importlib.util.find_spec('basic_pitch'); p=(pathlib.Path(s.origin).parent/'saved_models/icassp_2022/nmp.onnx') if s and s.origin else None; ok=bool(p and p.is_file() and torch.cuda.is_available() and 'CUDAExecutionProvider' in ort.get_available_providers()); print(json.dumps({'ok':ok,'device':torch.cuda.get_device_name(0) if ok else None,'providers':ort.get_available_providers()}))"
        try:
            result = subprocess.run([str(worker_python), "-c", probe], capture_output=True, text=True, timeout=20, check=False)
            info = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 and result.stdout.strip() else {}
            worker_model_ready, worker_device = info.get("ok", False), info.get("device")
        except Exception:
            worker_model_ready = False
    basic_pitch_spec = importlib.util.find_spec("basic_pitch")
    basic_pitch = worker_model_ready
    demucs_spec = importlib.util.find_spec("demucs")
    demucs = demucs_spec is not None
    cache = Path(os.environ.get("TORCH_HOME", Path.home() / ".cache" / "torch")) / "hub" / "checkpoints"
    demucs_map = {"htdemucs": {"955717e8"}, "htdemucs_ft": {"f7e0c4bc", "d12395a8", "92cfc3b6", "04573f0d"},
                  "htdemucs_6s": {"5c90dfd2"}}
    demucs_cuda = False
    demucs_device = None
    if demucs:
        try:
            import torch
            demucs_cuda = torch.cuda.is_available()
            if demucs_cuda:
                demucs_device = torch.cuda.get_device_name(torch.cuda.current_device())
        except Exception:
            demucs_cuda = False
    demucs_weights = {name: sorted(p.name for p in cache.glob("*.th") if p.name.split("-", 1)[0] in signatures) if cache.is_dir() else [] for name, signatures in demucs_map.items()}
    demucs_ready = demucs and demucs_cuda and any(demucs_weights.values())
    demucs_prerequisite = None if demucs_ready else ("CUDA-capable PyTorch and a cached supported Demucs checkpoint are required." if demucs and not demucs_cuda else "Install demucs and provision official pretrained checkpoints in TORCH_HOME/hub/checkpoints; weights are never fetched automatically.")
    return {
        "vocal_to_midi": {"available": basic_pitch, "engine": "Spotify Basic Pitch" if basic_pitch else None,
                          "worker_interpreter": str(worker_python) if worker_ready else None,
                          "gpu": worker_device,
                          "package_installed": bool(basic_pitch_spec), "model_file": "icassp_2022/nmp.onnx" if basic_pitch else None,
                          "prerequisite": None if basic_pitch else "External Python must have Basic Pitch packaged ONNX model and CUDAExecutionProvider available."},
        "stem_splitter": {"available": demucs_ready, "engine": "Demucs" if demucs else None,
                          "gpu": demucs_device if demucs_cuda else None,
                          "package_installed": demucs, "cached_model_checkpoints": demucs_weights,
                          "prerequisite": demucs_prerequisite},
        "ai_score": {"available": (ABC2VOCALS_DIR / "abc2vocals.py").is_file(), "engine": "abc2vocals configured chat completion"},
        "smart_accompaniment": {"available": (ABC2VOCALS_DIR / "abc2vocals.py").is_file(), "engine": "abc2vocals configured chat completion"},
    }


def _require_audio():
    body = request.get_json(silent=True) or {}
    encoded = body.get("audio_base64")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("audio_base64 is required (base64-encoded WAV, FLAC, MP3, or supported audio)")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ValueError("audio_base64 is invalid") from None
    if not raw:
        raise ValueError("audio is empty")
    if len(raw) > MAX_AUDIO_BYTES:
        raise ValueError(f"audio exceeds {MAX_AUDIO_BYTES} byte limit")
    return raw


def _vocal_to_midi():
    status = _model_status()["vocal_to_midi"]
    if not status["available"]:
        return jsonify({"error": "MODEL_UNAVAILABLE", "detail": status["prerequisite"], "status": status}), 503
    audio = _require_audio()
    worker_override = os.environ.get("SVS_MUSIC_AI_PYTHON")
    worker_python = Path(worker_override or sys.executable)
    if not worker_python.is_file() or (getattr(sys, "frozen", False) and not worker_override):
        raise RuntimeError("Basic Pitch requires a valid external Python; configure SVS_MUSIC_AI_PYTHON")
    worker_model_probe = "import importlib.util,json,pathlib; s=importlib.util.find_spec('basic_pitch'); p=(pathlib.Path(s.origin).parent/'saved_models/icassp_2022/nmp.onnx') if s and s.origin else None; print(json.dumps({'model':str(p) if p else None,'ok':bool(p and p.is_file())}))"
    probe = subprocess.run([str(worker_python), "-c", worker_model_probe], capture_output=True, text=True, timeout=20, check=False)
    worker_model = json.loads(probe.stdout.strip().splitlines()[-1]) if probe.returncode == 0 and probe.stdout.strip() else {}
    if not worker_model.get("ok"):
        raise RuntimeError("Basic Pitch model is missing from worker interpreter; configure SVS_MUSIC_AI_PYTHON")
    worker = r'''import json, pathlib, sys
sys.modules["jax"] = None
import torch
import onnxruntime as ort
if not torch.cuda.is_available() or "CUDAExecutionProvider" not in ort.get_available_providers():
    raise RuntimeError("CUDA-capable PyTorch and ONNX Runtime are required for GPU transcription")
from basic_pitch.inference import predict, Model
gpu_session = ort.InferenceSession(str(sys.argv[2]), providers=[("CUDAExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"])
active = gpu_session.get_providers()
if not active or active[0] != "CUDAExecutionProvider":
    raise RuntimeError(f"Basic Pitch ONNX did not activate CUDAExecutionProvider: {active}")
gpu_session.disable_fallback()
gpu_model = object.__new__(Model)
gpu_model.model_type = Model.MODEL_TYPES.ONNX
gpu_model.model = gpu_session
audio_path, model_path, result_path = map(pathlib.Path, sys.argv[1:4])
_, midi, _ = predict(str(audio_path), model_or_model_path=gpu_model)
events = []
for instrument in midi.instruments:
    for note in instrument.notes:
        tick = round(note.start * 960)
        end_tick = round(note.end * 960)
        events.append({"tick": tick, "duration": max(1, end_tick-tick), "key": int(note.pitch),
                       "velocity": float(note.velocity)/127.0, "lyric": "a", "legato": False})
midi.write(str(result_path.with_suffix(".mid")))
result_path.write_text(json.dumps({"notes": sorted(events,key=lambda n:(n["tick"],n["key"])),
                                  "providers": active, "device": "cuda:0"}),encoding="utf-8")
'''
    with tempfile.TemporaryDirectory(prefix="svs-basic-pitch-") as td:
        root = Path(td)
        audio_path, result_path = root / "input.wav", root / "result.json"
        audio_path.write_bytes(audio)
        proc = subprocess.run([str(worker_python), "-c", worker, str(audio_path), worker_model["model"], str(result_path)],
                              capture_output=True, text=True, timeout=600, check=False)
        if proc.returncode:
            raise RuntimeError(f"Basic Pitch worker failed ({proc.returncode}): {proc.stderr[-1200:]}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        midi_b64 = base64.b64encode(result_path.with_suffix(".mid").read_bytes()).decode("ascii")
    notes = result["notes"]
    return jsonify({"ok": True, "engine": "Spotify Basic Pitch", "midi_base64": midi_b64,
                    "note_count": len(notes), "notes": notes, "ppq": 480, "tempo_bpm": 120,
                    "device": result["device"], "providers": result["providers"]})


def _stem_split():
    status = _model_status()["stem_splitter"]
    if not status["available"]:
        return jsonify({"error": "MODEL_UNAVAILABLE", "detail": status["prerequisite"], "status": status}), 503
    audio = _require_audio()
    body = request.get_json(silent=True) or {}
    model_name = body.get("model", "htdemucs")
    if model_name not in ("htdemucs", "htdemucs_ft", "htdemucs_6s"):
        return jsonify({"error": "BAD_REQUEST", "detail": "model must be htdemucs, htdemucs_ft, or htdemucs_6s"}), 400
    import soundfile as sf
    import torch
    if not torch.cuda.is_available():
        return jsonify({"error": "GPU_UNAVAILABLE", "detail": "CUDA-capable PyTorch is required for local GPU stem separation"}), 503
    from demucs.apply import apply_model
    from demucs.audio import convert_audio
    with tempfile.TemporaryDirectory(prefix="svs-demucs-") as td:
        in_path = Path(td) / "input.wav"
        in_path.write_bytes(audio)
        wav, sr = sf.read(str(in_path), dtype="float32", always_2d=True)
        cache = Path(os.environ.get("TORCH_HOME", Path.home() / ".cache" / "torch")) / "hub" / "checkpoints"
        expected = {"htdemucs": "955717e8", "htdemucs_ft": "f7e0c4bc", "htdemucs_6s": "5c90dfd2"}[model_name]
        checkpoint = next(iter(cache.glob(expected + "*.th")), None)
        if checkpoint is None:
            return jsonify({"error": "MODEL_UNAVAILABLE", "detail": f"No cached Demucs checkpoint for {model_name}"}), 503
        from demucs.states import load_model
        model = load_model(torch.load(checkpoint, map_location="cpu", weights_only=False))
        device = torch.device("cuda:0")
        model.to(device)
        if next(model.parameters()).device.type != "cuda":
            raise RuntimeError("Demucs checkpoint failed to load on CUDA")
        source = torch.from_numpy(wav.T.copy()).to(device)
        source = convert_audio(source, sr, model.samplerate, model.audio_channels)
        with torch.no_grad():
            separated = apply_model(model, source[None], device=device, progress=False)[0]
        stems = {}
        for index, name in enumerate(model.sources):
            stem_path = Path(td) / f"{name}.wav"
            sf.write(str(stem_path), separated[index].cpu().numpy().T, model.samplerate, subtype="PCM_16")
            stems[name] = base64.b64encode(stem_path.read_bytes()).decode("ascii")
    return jsonify({"ok": True, "engine": f"Demucs {model_name}", "sample_rate": model.samplerate,
                    "device": str(device), "model_device": str(next(model.parameters()).device),
                    "stems": {name: {"audio_base64": data, "format": "wav"} for name, data in stems.items()}})


def _validate_abc(abc_text, voice=None):
    if not isinstance(abc_text, str) or not abc_text.strip():
        raise ValueError("abc must be a non-empty string")
    if len(abc_text) > MAX_ABC_CHARS:
        raise ValueError(f"abc exceeds {MAX_ABC_CHARS} character limit")
    a2v = _abc_module()
    voices = a2v.split_abc_voices(abc_text)["order"]
    selected_voice = voice or (voices[0] if voices else None)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".abc", delete=False) as f:
        f.write(abc_text)
        path = f.name
    try:
        parsed = a2v.parse_abc(path, voice=selected_voice)
    finally:
        Path(path).unlink(missing_ok=True)
    return a2v, parsed


def _llm_json(a2v, system_prompt, user_prompt):
    config = a2v.load_saved_llm_config()
    key = a2v.llm_api_key(config.get("key"))
    if not key:
        raise RuntimeError("No chat API key configured; set it in abc2vocals API settings or ABC2VOCALS_LLM_KEY/OPENAI_API_KEY")
    content = a2v._llm_chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        base_url=config.get("url") or a2v.DEFAULT_LLM_BASE_URL, api_key=key,
        model=config.get("model") or a2v.DEFAULT_LLM_MODEL, timeout=90.0, temperature=0.4,
    )
    if not isinstance(content, str) or len(content) > MAX_LLM_OUTPUT_CHARS:
        raise RuntimeError("chat completion returned an invalid or oversized response")
    content = a2v._strip_fence(content)
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        raise RuntimeError("chat completion did not return valid JSON") from None


def _parsed_counts(parsed):
    events = parsed.get("events", [])
    return sum(not event.get("is_rest") for event in events), sum(float(event.get("dur_q") or 0) for event in events)


def _parsed_notes(parsed):
    tick = 0
    notes = []
    for event in parsed.get("events", []):
        duration = max(1, round(float(event.get("dur_q") or 0) * 480))
        if not event.get("is_rest") and event.get("midi") is not None:
            notes.append({"tick": tick, "duration": duration, "key": int(event["midi"]), "velocity": 1.0,
                          "lyric": event.get("lyric") or "a", "legato": False})
        tick += duration
    return notes, tick


def _ai_score():
    body = request.get_json(silent=True) or {}
    abc_text = body.get("abc")
    if isinstance(abc_text, str) and abc_text.strip():
        a2v, parsed = _validate_abc(abc_text)
        prompt = (
            "Compose a concise, musically coherent continuation/arrangement as ABC notation. "
            "Return JSON only with string field abc. Preserve the input meter, key, tempo, "
            "and idiomatic ABC syntax. Do not return prose or markdown.\n\nINPUT ABC:\n" + abc_text
        )
        generated = _llm_json(a2v, "You are a music score arranger. Return only the requested JSON object.", prompt)
        result = generated.get("abc") if isinstance(generated, dict) else None
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("chat completion JSON must contain a non-empty abc string")
        _a2v, validated = _validate_abc(result)
        notes, total_ticks = _parsed_notes(validated)
        source_note_count, _ = _parsed_counts(parsed)
        return jsonify({"ok": True, "engine": "abc2vocals configured chat completion", "abc": result,
                        "note_count": len(notes), "source_note_count": source_note_count,
                        "notes": notes, "total_ticks": total_ticks, "bpm": validated.get("bpm", 120),
                        "beats": validated.get("beats", 4), "beat_type": validated.get("beat_type", 4),
                        "key": validated.get("key", 0)})

    theme = body.get("theme")
    if not isinstance(theme, str) or not theme.strip() or len(theme) > 500:
        raise ValueError("Provide either abc or a theme of at most 500 characters")
    bars = body.get("bars", 8)
    bpm = body.get("bpm", 100)
    meter = body.get("meter", "4/4")
    key = body.get("key", "C")
    if not isinstance(bars, int) or isinstance(bars, bool) or not 1 <= bars <= 64:
        raise ValueError("bars must be an integer from 1 to 64")
    if not isinstance(bpm, (int, float)) or isinstance(bpm, bool) or not 30 <= bpm <= 300:
        raise ValueError("bpm must be between 30 and 300")
    if not isinstance(meter, str) or not __import__("re").fullmatch(r"[1-9][0-9]?/[1-9][0-9]?", meter):
        raise ValueError("meter must be a numeric fraction such as 4/4 or 6/8")
    if not isinstance(key, str) or key not in {"C", "G", "D", "A", "E", "B", "F", "Bb", "Eb", "Ab", "Db", "Gb", "F#", "Am", "Em", "Bm", "F#m", "C#m", "G#m", "Dm", "Gm", "Cm", "Fm", "Bbm", "Ebm"}:
        raise ValueError("key is not a supported ABC major/minor key")
    a2v = _abc_module()
    prompt = (
        "Compose an original, singable song as complete ABC notation, with one melody voice and "
        "a `w:` lyric line aligned to every pitched note. Return JSON only with string field abc. "
        "The ABC must be directly importable, contain X:, T:, M:, L:, Q:, K: headers, exactly the "
        f"requested {bars} bars, a clear tonal melody, and lyrics in the requested language/theme. "
        "Use idiomatic ABC lengths and no commentary.\n"
        f"Theme: {theme.strip()}\nRequested tempo: {bpm} BPM; meter: {meter}; key: {key}; bars: {bars}."
    )
    generated = _llm_json(a2v, "You are a professional songwriter and ABC score composer. Return only the requested JSON object.", prompt)
    result = generated.get("abc") if isinstance(generated, dict) else None
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("chat completion JSON must contain a non-empty abc string")
    voices = a2v.split_abc_voices(result)["order"]
    if len(voices) > 1:
        raise RuntimeError("theme composition must contain exactly one vocal voice")
    _a2v, validated = _validate_abc(result)
    pitched = [event for event in validated.get("events", []) if not event.get("is_rest")]
    if not pitched or any(not event.get("lyric") for event in pitched):
        raise RuntimeError("generated composition must provide a lyric for every pitched note")
    numerator, denominator = (int(part) for part in meter.split("/"))
    expected_ticks = round(bars * numerator * 4 * 480 / denominator)
    notes, total_ticks = _parsed_notes(validated)
    if abs(total_ticks - expected_ticks) > 30:
        raise RuntimeError(f"generated song duration {total_ticks} ticks differs from requested {expected_ticks} ticks")
    actual_bars = max((line.count("|") for line in result.splitlines() if line and not line.lstrip().startswith(("%", "w:"))), default=0)
    if actual_bars and abs(actual_bars - bars) > 1:
        raise RuntimeError(f"generated ABC has {actual_bars} apparent bars; requested {bars}")
    return jsonify({"ok": True, "engine": "abc2vocals configured chat completion", "abc": result,
                    "theme": theme.strip(), "note_count": len(notes), "source_note_count": 0,
                    "notes": notes, "total_ticks": total_ticks, "bpm": validated.get("bpm", bpm),
                    "beats": validated.get("beats", 4), "beat_type": validated.get("beat_type", 4),
                    "key": validated.get("key", 0), "bars": bars})


def _smart_accompaniment():
    body = request.get_json(silent=True) or {}
    a2v, parsed = _validate_abc(body.get("abc"))
    instruments = body.get("style", "piano accompaniment")
    if not isinstance(instruments, str) or not instruments.strip() or len(instruments) > 200:
        raise ValueError("style must be a non-empty string of at most 200 characters")
    source_notes, source_ticks = _parsed_notes(parsed)
    prompt = (
        "Create a musically coherent accompaniment as a separate ABC voice. Return JSON only "
        "with string field abc containing a complete valid ABC tune and an accompaniment voice "
        "named Accomp. Preserve key, meter, tempo, bar structure, and duration of the supplied "
        f"score; the accompaniment voice must total exactly {source_ticks / 480:g} quarter-note beats "
        f"({source_ticks} ticks at 480 PPQ). Do not repeat or extend the supplied score. "
        "Write playable accompaniment, not lyrics. Style: " + instruments + "\n\nSCORE:\n" + body["abc"]
    )
    generated = _llm_json(a2v, "You are an experienced arranger. Return only the requested JSON object.", prompt)
    result = generated.get("abc") if isinstance(generated, dict) else None
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("chat completion JSON must contain a non-empty abc string")
    score_parts = a2v.split_abc_voices(result)
    voice_names = {voice: (score_parts["decls"].get(voice, "") + " " + " ".join(score_parts["vheaders"].get(voice, []))).lower() for voice in score_parts["order"]}
    accomp_voice = next((voice for voice, declaration in voice_names.items() if "accomp" in declaration), None)
    if accomp_voice is None:
        raise RuntimeError("generated ABC must contain an Accomp voice")
    _a2v, accompaniment = _validate_abc(result, voice=accomp_voice)
    accomp_notes, accomp_ticks = _parsed_notes(accompaniment)
    if abs(accomp_ticks - source_ticks) > 30:
        raise RuntimeError(f"accompaniment duration differs from source by {abs(accomp_ticks-source_ticks)} ticks")
    return jsonify({"ok": True, "engine": "abc2vocals configured chat completion", "abc": result,
                    "note_count": len(accomp_notes), "source_note_count": len(source_notes),
                    "notes": accomp_notes, "total_ticks": accomp_ticks})


def register_routes(app):
    """Register optional music-AI routes on the Flask app; no model weights fetched."""
    @app.get("/music-ai/status")
    def music_ai_status():
        return jsonify({"ok": True, "features": _model_status()})

    def endpoint(fn):
        def run():
            try:
                return fn()
            except ValueError as exc:
                return jsonify({"error": "BAD_REQUEST", "detail": str(exc)}), 400
            except Exception as exc:  # noqa: BLE001 — model/config failures become explicit API errors
                return jsonify({"error": "MUSIC_AI_UNAVAILABLE", "detail": f"{type(exc).__name__}: {exc}"}), 503
        run.__name__ = "music_ai_" + fn.__name__
        return run

    app.add_url_rule("/music-ai/vocal-to-midi", view_func=endpoint(_vocal_to_midi), methods=["POST"])
    app.add_url_rule("/music-ai/stems", view_func=endpoint(_stem_split), methods=["POST"])
    app.add_url_rule("/music-ai/score", view_func=endpoint(_ai_score), methods=["POST"])
    app.add_url_rule("/music-ai/accompaniment", view_func=endpoint(_smart_accompaniment), methods=["POST"])
