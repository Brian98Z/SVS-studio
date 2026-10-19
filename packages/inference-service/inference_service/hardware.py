"""ONNX Runtime provider inspection and explicit per-reader device setup."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROVIDERS = {
    "cuda": "CUDAExecutionProvider",
    "directml": "DmlExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
    "cpu": "CPUExecutionProvider",
}
PREFERENCE_ORDER = ("cuda", "directml", "openvino", "cpu")
VALID_PREFERENCES = frozenset(("auto", *PROVIDERS))
_last_report: dict[str, Any] = {"preference": "auto", "sessions": []}


def inspect_providers() -> dict[str, Any]:
    """Report installed ORT providers, without implying they can load a model."""
    try:
        import onnxruntime as ort
    except ImportError as exc:
        return {"available": [], "error": str(exc)}
    available = list(ort.get_available_providers())
    return {
        "available": available,
        "providers": {
            name: {"execution_provider": provider, "available": provider in available}
            for name, provider in PROVIDERS.items()
        },
    }


def _readers(pred: Any) -> list[Any]:
    """Collect the existing OnnxReader instances from PredAll's five components."""
    from diffsinger_utau.voice_bank.commons.voice_bank_reader import OnnxReader

    result: list[Any] = []
    seen_objects: set[int] = set()
    seen_readers: set[int] = set()
    stack = [getattr(pred, name, None) for name in
             ("dsdur", "dspitch", "dsvariance", "dsacoustic", "dsvocoder")]
    while stack:
        value = stack.pop()
        if value is None or id(value) in seen_objects:
            continue
        seen_objects.add(id(value))
        if isinstance(value, OnnxReader):
            if id(value) not in seen_readers:
                result.append(value)
                seen_readers.add(id(value))
            continue
        if isinstance(value, (str, bytes, Path, int, float, bool)):
            continue
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, (list, tuple, set)):
            stack.extend(value)
        elif hasattr(value, "__dict__"):
            stack.extend(vars(value).values())
    return result


def _providers_for(preference: str, available: list[str]) -> list[str]:
    if preference == "auto":
        candidates = [PROVIDERS[name] for name in PREFERENCE_ORDER]
    else:
        candidates = [PROVIDERS[preference]]
    candidates = [name for name in candidates if name in available]
    if preference == "auto":
        if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in candidates:
            candidates.append("CPUExecutionProvider")
    elif preference == "cpu":
        candidates = ["CPUExecutionProvider"] if "CPUExecutionProvider" in available else []
    return candidates


def _load_session(reader: Any, providers: list[str]) -> tuple[Any, list[str]]:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    session = ort.InferenceSession(str(reader.onnx_path), sess_options=options, providers=providers)
    loaded = list(session.get_providers())
    return session, loaded


def validate_preference(value: str) -> str:
    """Normalize and validate public device preference values."""
    preference = str(value).strip().lower()
    if preference not in VALID_PREFERENCES:
        raise ValueError(f"Unknown inference device preference: {preference}")
    return preference


def setup_runtime(pred: Any, preference: str = "auto", a2v: Any = None) -> dict[str, Any]:
    """Load existing voicebank readers using requested providers, proving use by readback.

    A provider is selected only when a new session reports it from get_providers().
    Failed auto candidates are tried in order; CPU is the final baseline.
    """
    global _last_report
    preference = validate_preference(preference)
    import onnxruntime as ort

    available = list(ort.get_available_providers())
    candidates = _providers_for(preference, available)
    if not candidates:
        raise RuntimeError(f"No ONNX Runtime execution provider available for preference: {preference}")
    if preference in ("cuda", "auto") and "CUDAExecutionProvider" in candidates and a2v is not None:
        # Reuse only the plugin's DLL finder; avoid its process-wide reader monkeypatch.
        ensure = getattr(a2v, "ensure_cuda_dlls", None)
        if ensure is not None:
            ensure()
    readers = _readers(pred)
    if not readers:
        raise RuntimeError("No voicebank ONNX readers were found for hardware setup")
    sessions: list[dict[str, Any]] = []
    for reader in readers:
        errors: list[str] = []
        selected = None
        for candidate in candidates:
            requested = [candidate]
            if candidate != "CPUExecutionProvider" and "CPUExecutionProvider" in available:
                requested.append("CPUExecutionProvider")
            try:
                session, loaded = _load_session(reader, requested)
            except Exception as exc:  # provider/model incompatibility, continue only for auto
                errors.append(f"{candidate}: {exc}")
                if preference != "auto":
                    break
                continue
            if candidate in loaded:
                selected = (session, loaded, candidate)
                break
            errors.append(f"{candidate}: session did not report requested provider (loaded {loaded})")
            if preference != "auto":
                break
        if selected is None:
            raise RuntimeError(f"No requested inference provider loaded for {reader.onnx_path}: {'; '.join(errors)}")
        session, loaded, chosen = selected
        reader.session = session
        reader.input_names = [item.name for item in session.get_inputs()]
        reader.output_names = [item.name for item in session.get_outputs()]
        reader.input_shapes = {item.name: item.shape for item in session.get_inputs()}
        reader.output_shapes = {item.name: item.shape for item in session.get_outputs()}
        reader.model_size = os.path.getsize(reader.onnx_path)
        sessions.append({
            "model": Path(reader.onnx_path).name,
            "requested_provider": PROVIDERS[preference] if preference != "auto" else "auto",
            "provider": chosen,
            "loaded_providers": loaded,
            "errors": errors,
        })
    _last_report = {
        "preference": preference,
        "available": available,
        "sessions": sessions,
        "selected_providers": sorted({item["provider"] for item in sessions}),
        "provider": (sessions[0]["provider"] if sessions and len({item["provider"] for item in sessions}) == 1 else "mixed"),
        "status": "loaded",
    }
    return dict(_last_report)


def runtime_status() -> dict[str, Any]:
    report = dict(_last_report)
    report["providers"] = inspect_providers()
    return report
def publish_runtime(report: dict[str, Any]) -> None:
    """Publish the last fully verified predictor report for the status route."""
    global _last_report
    _last_report = dict(report)


def register_routes(app: Any) -> None:
    """Register GET /hardware provider availability and current runtime status."""
    from flask import jsonify, request

    @app.get("/hardware")
    def hardware_status():
        preference = request.args.get("device")
        if preference is not None:
            try:
                validate_preference(preference)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 422
        return jsonify({"ok": True, **runtime_status()})
