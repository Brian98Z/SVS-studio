"""HTTP bridge to the standalone language-aware G2P package."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from threading import Lock

_ENGINE = None
_LOCK = Lock()


def _load_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
        package_dir = Path(os.environ.get("SVS_G2P_DIR", Path(__file__).resolve().parents[2] / "g2p-engine"))
        module_path = package_dir / "g2p.py"
        spec = importlib.util.spec_from_file_location("svs_g2p_engine", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load G2P engine from {module_path}")
        module = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _ENGINE = module.G2P(package_dir / "dicts")
    return _ENGINE


def register_routes(app):
    """Register POST /g2p, accepting text plus optional language or phoneme override."""
    from flask import jsonify, request

    @app.post("/g2p")
    def g2p_convert():
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            return jsonify({"error": "BAD_REQUEST", "detail": "text must be a string"}), 400
        language = body.get("language")
        phonemes = body.get("phonemes")
        syllables = body.get("syllables")
        if language is not None and not isinstance(language, str):
            return jsonify({"error": "BAD_REQUEST", "detail": "language must be a string"}), 400
        if phonemes is not None and (not isinstance(phonemes, list) or any(not isinstance(p, str) for p in phonemes)):
            return jsonify({"error": "BAD_REQUEST", "detail": "phonemes must be an array of strings"}), 400
        if syllables is not None and (not isinstance(syllables, list) or any(not isinstance(part, str) for part in syllables)):
            return jsonify({"error": "BAD_REQUEST", "detail": "syllables must be an array of strings"}), 400
        try:
            engine = _load_engine()
            if syllables is not None:
                if not language:
                    return jsonify({"error": "BAD_REQUEST", "detail": "language is required with syllables"}), 400
                result = {"ok": True, "per_note": engine.convert_per_note(syllables, language), "phonemes": engine.convert(body["text"], language=language)}
            else:
                result = {"ok": True, "phonemes": engine.convert(body["text"], language=language, phonemes=phonemes)}
        except ValueError as exc:
            return jsonify({"error": "BAD_REQUEST", "detail": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": "INTERNAL", "detail": str(exc)}), 500
        return jsonify(result)
