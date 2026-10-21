"""User-managed voicebank registry and conservative local model capability inspection.

Registry entries only reference configured directories; removal never removes model files.
"""
from __future__ import annotations

import hashlib
import json
import os
import math
import re
import threading
from pathlib import Path
from typing import Any

from flask import jsonify, request

from . import config

_REGISTRY_LOCK = threading.RLock()


def _config_path() -> Path:
    override = os.environ.get("SVS_VOICE_REGISTRY")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
        return root / "SVS Studio" / "voices.json"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "svs-studio" / "voices.json"


def _load() -> list[dict[str, Any]]:
    path = _config_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        entries = value.get("voices", []) if isinstance(value, dict) else []
        return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)
                and isinstance(entry.get("path"), str)] if isinstance(entries, list) else []
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        return []


def _save(voices: list[dict[str, Any]]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps({"version": 1, "voices": voices}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _identity(path: Path) -> str:
    # Path identity (not model bytes): stable across scans, deterministic for this machine's canonical path.
    return "voice." + hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()[:20]


def inspect_voice(path_value: str, name: str | None = None, vocoder_path: str | None = None) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("Voicebank directory does not exist or is not a directory")
    files = sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: str(p).casefold())
    # Avoid unbounded traversal in accidental huge roots.
    files = files[:10000]
    rel = lambda p: str(p.relative_to(path)).replace("\\", "/")
    metadata_files = [p for p in files if p.name.casefold() in {
        "config.json", "voice.json", "metadata.json", "speaker.json", "manifest.json",
        "character.txt", "README.md", "license", "license.txt", "license.md", "LICENSE",
    }]
    docs: list[tuple[str, Any]] = []
    for file in metadata_files:
        if file.suffix.casefold() == ".json":
            try:
                docs.append((rel(file), json.loads(file.read_text(encoding="utf-8"))))
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
    flat: dict[str, Any] = {}
    for _, doc in docs:
        if isinstance(doc, dict):
            for k, v in doc.items():
                flat.setdefault(str(k).casefold(), v)
    speaker_values = flat.get("speakers", flat.get("speaker", flat.get("spk_id_map", [])))
    speakers = []
    if isinstance(speaker_values, dict):
        speakers = [{"id": str(k), "name": str(v)} for k, v in speaker_values.items()]
    elif isinstance(speaker_values, list):
        speakers = [{"id": str(i), "name": str(v)} for i, v in enumerate(speaker_values)]
    elif isinstance(speaker_values, str):
        speakers = [{"id": "0", "name": speaker_values}]
    license_files = [p for p in files if "license" in p.name.casefold() or p.name.casefold() in {"readme.md", "license.md"}]
    licenses = []
    for f in license_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:12000]
            licenses.append({"file": rel(f), "text": text})
        except OSError:
            continue
    exts: dict[str, list[str]] = {}
    for p in files:
        if p.suffix.casefold() in {".onnx", ".pth", ".pt", ".ckpt", ".safetensors", ".yaml", ".yml", ".json"}:
            exts.setdefault(p.suffix.casefold(), []).append(rel(p))
    is_diffsinger = any(p.name.casefold() in {"dsconfig.yaml", "dsconfig.yml"} for p in files)
    model_files = exts.get(".onnx", []) + exts.get(".pth", []) + exts.get(".pt", []) + exts.get(".ckpt", []) + exts.get(".safetensors", [])
    advertised: set[str] = set()
    family = "DiffSinger" if is_diffsinger else (str(flat.get("model_type", flat.get("model", "unknown"))) if model_files else "metadata-only")
    ds_required = ["dsconfig.yaml", "dsdur/dsdict-zh.yaml", "dspitch/dsconfig.yaml", "dsvariance/dsconfig.yaml"]
    ds_missing = [relpath for relpath in ds_required if not (path / relpath).is_file()]
    from .vocoder_binding import inspect_vocoder
    vocoder = inspect_vocoder(path, vocoder_path)
    if not vocoder["compatible"]:
        ds_missing.extend(vocoder["missing_files"])
    ds_missing = list(dict.fromkeys(ds_missing))
    if is_diffsinger:
        from .vocoder_binding import validate_dsconfig_references
        try:
            ds_missing.extend(validate_dsconfig_references(path))
        except Exception as exc:
            ds_missing.append(f"dsconfig.yaml:{type(exc).__name__}:{exc}")
    ds_missing = list(dict.fromkeys(ds_missing))
    if is_diffsinger:
        advertised.add("diffsinger_native")
    ds_config_text = ""
    declared_speakers: set[str] = set()
    component_speakers: list[set[str]] = []
    for cfg in files:
        if cfg.name.casefold() in {"dsconfig.yaml", "dsconfig.yml"}:
            try:
                text = cfg.read_text(encoding="utf-8", errors="replace")
                ds_config_text += text + "\n"
                values = re.search(r"(?m)^speakers:\s*\[([^]]*)\]", text)
                if values:
                    names = {item.strip().strip("'\\\"") for item in values.group(1).split(",") if item.strip()}
                    if names:
                        component_speakers.append(names)
                        declared_speakers.update(names)
            except OSError:
                pass
    if declared_speakers:
        speakers = [{"id": name, "name": name} for name in sorted(declared_speakers)]
        if len(component_speakers) == 4 and set.intersection(*component_speakers):
            advertised.update(("spk_id", "speaker_embedding_blend"))
    for flag, capability in (("use_breathiness_embed", "breathiness"), ("use_voicing_embed", "voicing"),
                             ("use_key_shift_embed", "key_shift"), ("use_speed_embed", "speed")):
        if re.search(rf"(?m)^{flag}:\s*true\s*$", ds_config_text, re.I):
            advertised.add(capability)
    vits_hint = "vits" in family.casefold() or any("vits" in x.casefold() for x in exts.get(".onnx", []))
    staged_runtime = (config.ABC2VOCALS_DIR / 'abc2vocals.py').is_file() and config.GPU_PYTHON.is_file()
    prerequisites = {"directory_readable": os.access(path, os.R_OK), "model_files_found": bool(model_files),
                     "diffsinger_topology_complete": bool(is_diffsinger and not ds_missing and vocoder["compatible"]),
                     "diffsinger_missing_files": ds_missing if is_diffsinger else [],
                     "native_runtime_staged": staged_runtime if is_diffsinger else False,
                     "vits_model_hint": vits_hint,
                     "vits_adapter": {"available": False, "reason": "No verified model-specific VITS inference adapter is implemented."}}
    ready = bool(prerequisites["diffsinger_topology_complete"] and staged_runtime)
    return {
        "id": _identity(path), "name": name or str(flat.get("name", path.name)),
        "path": str(path), "family": family, "registered": False,
        "metadata_files": [rel(p) for p in metadata_files], "model_files": model_files,
        "model_file_types": {k: v for k, v in exts.items() if k != ".json"},
        "speakers": speakers, "capabilities": sorted(advertised), "licenses": licenses,
        "license_status": "declared" if licenses else "unknown",
        "prerequisites": prerequisites, "inference_ready": ready,
        "vocoder_path": str(Path(vocoder_path).expanduser().resolve()) if vocoder_path else None,
        "vocoder": vocoder,
    }



def resolve_voice(voice_id: str) -> dict[str, Any] | None:
    """Return registered descriptor refreshed from disk, for native-pipeline integration."""
    if voice_id == "diffsinger.qixuan":
        if config.VOICEBANK is None:
            return {"id": voice_id, "name": "外部 DiffSinger 音源", "family": "DiffSinger", "registered": True,
                    "inference_ready": False, "capabilities": [], "speakers": [], "licenses": [],
                    "prerequisites": {"reason": "Set SVS_VOICEBANK to an explicitly selected, licensed voicebank directory."}}
        try:
            builtin = inspect_voice(str(config.VOICEBANK), "外部 DiffSinger 音源")
        except (ValueError, OSError):
            return {"id": voice_id, "name": "外部 DiffSinger 音源", "path": str(config.VOICEBANK),
                    "family": "DiffSinger", "registered": True, "inference_ready": False,
                    "capabilities": [], "speakers": [], "licenses": [],
                    "prerequisites": {"reason": "Configured voicebank is unavailable."}}
        builtin["id"] = voice_id
        builtin["registered"] = True
        return builtin
    entry = next((v for v in _load() if v.get("id") == voice_id), None)
    if not entry:
        return None
    try:
        refreshed = inspect_voice(entry["path"], entry.get("name"), entry.get("vocoder_path"))
        refreshed["registered"] = True
        return refreshed
    except (ValueError, OSError, KeyError):
        return {**entry, "registered": True, "inference_ready": False,
                "prerequisites": {"directory_readable": False, "reason": "Configured directory is missing or inaccessible."}}


def validate_speaker_blend(voice_ids: list[str], weights: list[float]) -> dict[str, float]:
    """Validate same-model embedding blend; returns normalized weights."""
    if not voice_ids or len(voice_ids) != len(weights) or len(voice_ids) > 8:
        raise ValueError("Provide 1-8 voice IDs and exactly one weight per voice")
    if any(not isinstance(w, (int, float)) or not math.isfinite(float(w)) or not 0 < float(w) <= 1 for w in weights):
        raise ValueError("Blend weights must be finite numbers in (0, 1]")
    voices = [resolve_voice(v) for v in voice_ids]
    if any(v is None for v in voices):
        raise ValueError("Every blended voice must be registered")
    if len({v["path"] for v in voices if v}) != 1:
        raise ValueError("Speaker embedding blend is restricted to speakers in one compatible model")
    v = voices[0]
    if "speaker_embedding_blend" not in v["capabilities"]:
        raise ValueError("Model metadata does not declare compatible speaker embeddings")
    total = sum(map(float, weights))
    if not total or abs(total - 1) > 1e-5:
        raise ValueError("Blend weights must sum to 1")
    return dict(zip(voice_ids, map(float, weights)))


def register_voice(path: str, name: str | None = None) -> dict[str, Any]:
    voice = inspect_voice(path, name)
    with _REGISTRY_LOCK:
        entries = _load()
        previous = next((entry for entry in entries if entry.get("id") == voice["id"]), None)
        if previous and previous.get("vocoder_path"):
            voice = inspect_voice(path, name or previous.get("name"), previous["vocoder_path"])
        entries = [v for v in entries if v.get("id") != voice["id"]]
        entries.append({"id": voice["id"], "name": voice["name"], "path": voice["path"], "vocoder_path": voice["vocoder_path"]})
        _save(entries)
    voice["registered"] = True
    return voice


def register_routes(app) -> None:
    @app.get("/voices")
    def list_voices():
        result = []
        for entry in _load():
            voice = resolve_voice(entry.get("id", ""))
            if voice:
                result.append(voice)
        return jsonify({"voices": result, "registry_file": str(_config_path())})

    @app.post("/voices/inspect")
    def inspect_route():
        body = request.get_json(force=True, silent=True) or {}
        try:
            return jsonify(inspect_voice(body.get("path", ""), body.get("name")))
        except ValueError as e:
            return jsonify({"error": "BAD_REQUEST", "detail": str(e)}), 400

    @app.post("/voices")
    def register_route():
        body = request.get_json(force=True, silent=True) or {}
        try:
            voice = register_voice(body.get("path", ""), body.get("name"))
        except ValueError as e:
            return jsonify({"error": "BAD_REQUEST", "detail": str(e)}), 400
        return jsonify(voice), 201

    @app.patch("/voices/<voice_id>/vocoder")
    def bind_vocoder_route(voice_id):
        body = request.get_json(force=False, silent=True)
        if not isinstance(body, dict) or "path" not in body:
            return jsonify({"error": "BAD_REQUEST", "detail": "Expected JSON object with path (string or null)."}), 400
        path_value = body["path"]
        if path_value is not None and (not isinstance(path_value, str) or not path_value.strip()):
            return jsonify({"error": "BAD_REQUEST", "detail": "path must be a non-empty string or null."}), 400
        if path_value is not None and body.get("acknowledged_license") is not True:
            return jsonify({"error": "BAD_REQUEST", "detail": "Acknowledge that vocoder licensing is independent; this does not grant permission."}), 400
        with _REGISTRY_LOCK:
            entries = _load()
            entry = next((item for item in entries if item.get("id") == voice_id), None)
            if entry is None:
                return jsonify({"error": "NOT_FOUND", "detail": "Voice is not registered"}), 404
            try:
                from .vocoder_binding import validate_binding
                validated = validate_binding(Path(entry["path"]), path_value) if path_value is not None else None
            except (ValueError, OSError) as exc:
                return jsonify({"error": "BAD_REQUEST", "detail": str(exc)}), 400
            entry["vocoder_path"] = str(validated) if validated else None
            _save(entries)
        return jsonify(resolve_voice(voice_id))

    @app.delete("/voices/<voice_id>")
    def remove_route(voice_id):
        with _REGISTRY_LOCK:
            entries = _load()
            kept = [v for v in entries if v.get("id") != voice_id]
            if len(kept) == len(entries):
                return jsonify({"error": "NOT_FOUND", "detail": "Voice is not registered"}), 404
            _save(kept)
        return jsonify({"ok": True, "id": voice_id, "assets_deleted": False})
