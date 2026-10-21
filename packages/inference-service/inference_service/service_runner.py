"""推理服务 HTTP 入口。

启动: python -m inference_service.service_runner [--port 8760]
由 Rust server 子进程拉起；也可独立运行调试。
"""
from __future__ import annotations

import argparse
import base64 as b64mod
import time
from pathlib import Path

from flask import Flask, jsonify, request

from . import contract
from .cache_manager import AudioCache
from .onnx_pipeline import OnnxPipeline, MODELS_DIR

app = Flask(__name__)
cache = AudioCache()


@app.before_request
def local_browser_boundary():
    # Binding loopback and configuring CORS alone do not stop simple POSTs or
    # DNS rebinding. Check before any cache, file, plugin or project operation.
    if request.host not in {f"127.0.0.1:{LOCAL_PORT}", f"localhost:{LOCAL_PORT}"}:
        return jsonify({"error": "UNTRUSTED_HOST"}), 403
    origin = request.headers.get("Origin")
    allowed = {"http://tauri.localhost", "http://127.0.0.1:8761", "http://localhost:8761",
               "http://127.0.0.1:5173", "http://localhost:5173",
               f"http://127.0.0.1:{LOCAL_PORT}", f"http://localhost:{LOCAL_PORT}"}
    if origin is not None and origin not in allowed:
        return jsonify({"error": "UNTRUSTED_ORIGIN"}), 403
    if origin is None and request.headers.get("Sec-Fetch-Site") not in {None, "none", "same-origin", "same-site"}:
        return jsonify({"error": "UNTRUSTED_ORIGIN"}), 403


LOCAL_PORT = 8760

from .voices import register_routes as register_voices
from .vst_host import register_routes as register_vst
from .media_io import register_routes as register_media
register_voices(app)
from .voice_install import register_routes as register_voice_install
register_voice_install(app)
from .voice_distribution import register_routes as register_distribution
register_distribution(app)
register_vst(app)
from .vst_editor import register_routes as register_editor
register_editor(app)
register_media(app)
from .g2p_api import register_routes as register_g2p
register_g2p(app)
from .mixer import register_routes as register_mixer
from .music_ai import register_routes as register_music_ai
register_mixer(app)
register_music_ai(app)
from .realtime_mixer import register_routes as register_realtime_mixer
register_realtime_mixer(app)
from .hardware import register_routes as register_hardware
register_hardware(app)
from .interchange_api import register_routes as register_interchange
register_interchange(app)
_pipeline = None
_mode = "dsp"


def _init_mode():
    global _pipeline, _mode
    try:
        _pipeline = OnnxPipeline(MODELS_DIR)
        _mode = "onnx"
    except Exception as e:  # noqa: BLE001 — 任一模型缺失/损坏都回退
        _pipeline = None
        _mode = "dsp"
        print(f"[inference] ONNX unavailable ({e}); using DSP fallback", flush=True)


@app.get("/health")
def health():
    return jsonify({"ok": True, "mode": _mode, "sample_rate": contract.SR,
                    "cache": cache.stats()})


@app.post("/synth_phrase")
def synth_phrase():
    body = request.get_json(force=True, silent=True) or {}
    h = body.get("hash")
    if not h or "notes" not in body:
        return jsonify({"error": "BAD_REQUEST", "detail": "hash and notes required"}), 400

    cached = cache.get(h)
    if cached is not None:
        import base64 as b64mod
        return jsonify({"ok": True, "audio": b64mod.b64encode(cached.tobytes()).decode("ascii"),
                        "sample_rate": contract.SR, "cache": "hit", "elapsed_ms": 0})

    t0 = time.perf_counter()
    from .pipeline import synth_phrase
    try:
        pcm = synth_phrase(body, _mode, _pipeline)
    except ValueError as e:
        return jsonify({"error": "UNSUPPORTED_CONTROLS", "detail": str(e)}), 422
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "INTERNAL", "detail": str(e)}), 500
    elapsed = int((time.perf_counter() - t0) * 1000)
    cache.put(h, pcm)
    import base64 as b64mod
    return jsonify({"ok": True, "audio": b64mod.b64encode(pcm.tobytes()).decode("ascii"),
                    "sample_rate": contract.SR, "cache": "miss", "elapsed_ms": elapsed})


@app.post("/invalidate")
def invalidate():
    body = request.get_json(force=True, silent=True) or {}
    removed = cache.invalidate(body.get("hashes"), body.get("all", False))
    return jsonify({"ok": True, "removed": removed})


@app.post("/render_ds")
def render_ds():
    """真 DiffSinger 整轨渲染（abc2diffsinger 音源）。首次调用加载模型 ~20s。"""
    body = request.get_json(force=True, silent=True) or {}
    h = body.get("hash")
    notes = body.get("notes") or []
    if not h or not notes:
        return jsonify({"error": "BAD_REQUEST", "detail": "hash and notes required"}), 400
    try:
        from .render_ds import render_track
        pcm, hit = render_track(notes, body.get("tempos") or [], h,
                                int(body.get("key_shift") or 0), body.get("curves") or {},
                                int(body.get('offset_ticks') or 0),
                                body.get('singer') or 'diffsinger.qixuan', body.get('voice') or {})
    except ValueError as e:
        return jsonify({"error": "UNSUPPORTED_CONTROLS", "detail": str(e)}), 422
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return jsonify({"error": "INTERNAL", "detail": str(e)}), 500
    return jsonify({"ok": True, "audio": b64mod.b64encode(pcm.tobytes()).decode("ascii"),
                    "sample_rate": 24000, "cache": "hit" if hit else "miss"})


def main():
    global _mode
    global LOCAL_PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8760)
    args = ap.parse_args()
    LOCAL_PORT = args.port
    _init_mode()
    from waitress import serve
    serve(app, host="127.0.0.1", port=args.port, threads=8,
          max_request_body_size=140 * 1024 * 1024,
          clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    main()
