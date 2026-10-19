"""Optional offline VST3 effect hosting.

Discovery is filesystem-only. Constructing :class:`VST3Plugin` loads native plugin
code, so callers must explicitly pass the exact plugin bundle path to
:func:`open_effect`; there is deliberately no implicit scan-and-load operation.
Pedalboard hosts audio processing and parameter access, but does not expose a
portable plug-in editor/GUI contract here.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Iterable

import numpy as np


class VSTHostError(RuntimeError):
    """Raised for invalid bundles, unsupported runtimes, or plugin failures."""


class VSTRuntimeUnavailable(VSTHostError):
    """Raised when the optional Pedalboard host dependency is not installed."""


class PluginParameterError(VSTHostError):
    """Raised when a plugin parameter is missing or cannot accept a value."""


MAX_BLOCK_FRAMES = 65536


def _pedalboard():
    try:
        import pedalboard
    except ImportError as exc:
        raise VSTRuntimeUnavailable(
            "VST3 hosting requires the optional 'pedalboard' dependency; "
            "install packages/inference-service/requirements-vst.txt"
        ) from exc
    return pedalboard


def _validated_bundle(path: str | os.PathLike[str]) -> Path:
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path).strip():
        raise VSTHostError("An explicit VST3 bundle path is required")
    bundle = Path(path).expanduser().resolve(strict=True)
    if bundle.suffix.lower() != ".vst3" or not bundle.is_dir():
        raise VSTHostError("Plugin path must identify an existing .vst3 bundle directory")
    # VST3 bundles contain platform-specific binaries inside a directory; verify
    # there's at least one regular file without loading or inspecting its code.
    try:
        next(p for p in bundle.rglob("*") if p.is_file())
    except StopIteration as exc:
        raise VSTHostError("VST3 bundle is empty") from exc
    return bundle


def discover_vst3(search_roots: Iterable[str | os.PathLike[str]] | None = None) -> list[dict[str, str]]:
    """List VST3 bundle directories without loading or executing plugin code.

    Results are paths and directory names only; plugin metadata such as its
    vendor/name/parameters requires instantiation and therefore is not scanned.
    """
    if search_roots is None:
        if os.name == "nt":
            roots = [Path(os.environ.get("CommonProgramFiles", r"C:\Program Files\Common Files")) / "VST3"]
            local = os.environ.get("LOCALAPPDATA")
            if local:
                roots.append(Path(local) / "Programs" / "Common" / "VST3")
        elif __import__("sys").platform == "darwin":
            roots = [Path("/Library/Audio/Plug-Ins/VST3"), Path.home() / "Library/Audio/Plug-Ins/VST3"]
        else:
            roots = [Path("/usr/lib/vst3"), Path("/usr/local/lib/vst3"), Path.home() / ".vst3"]
    else:
        roots = [Path(p).expanduser() for p in search_roots]

    found: dict[str, dict[str, str]] = {}
    for root in roots:
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.suffix.lower() == ".vst3" and entry.is_dir():
                    resolved = entry.resolve()
                    found[str(resolved)] = {"name": entry.name, "path": str(resolved)}
        except OSError:
            continue
    return [found[key] for key in sorted(found, key=str.casefold)]


def open_effect(path: str | os.PathLike[str]):
    """Instantiate one explicitly selected VST3 bundle (native code execution)."""
    bundle = _validated_bundle(path)
    runtime = _pedalboard()
    try:
        # VST3Plugin construction is intentionally the only loading path here.
        return runtime.load_plugin(str(bundle))
    except Exception as exc:
        raise VSTHostError(f"Could not instantiate VST3 plugin at {bundle}: {exc}") from exc


def enumerate_parameters(plugin: Any) -> list[dict[str, Any]]:
    """Return parameter names, current values, and known numeric bounds."""
    try:
        parameters = plugin.parameters
    except Exception as exc:
        raise VSTHostError(f"Plugin parameter enumeration failed: {exc}") from exc
    result = []
    for name, parameter in parameters.items():
        item: dict[str, Any] = {"name": str(name)}
        for key in ("value", "min_value", "max_value"):
            value = getattr(parameter, key, None)
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                item[key] = float(value)
        result.append(item)
    return result


def set_parameter(plugin: Any, name: str, value: float) -> None:
    """Set one exposed plugin parameter after checking its reported bounds."""
    if not isinstance(name, str) or not name:
        raise PluginParameterError("Parameter name must be a non-empty string")
    try:
        parameter = plugin.parameters.get(name)
    except Exception as exc:
        raise VSTHostError(f"Plugin parameter enumeration failed: {exc}") from exc
    if parameter is None:
        raise PluginParameterError(f"Plugin has no parameter named {name!r}")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PluginParameterError("Parameter value must be finite and numeric") from exc
    if not math.isfinite(numeric):
        raise PluginParameterError("Parameter value must be finite and numeric")
    low, high = getattr(parameter, "min_value", None), getattr(parameter, "max_value", None)
    if low is not None and numeric < low or high is not None and numeric > high:
        raise PluginParameterError(f"Value {numeric} outside parameter range [{low}, {high}]")
    try:
        setattr(plugin, name, numeric)
    except Exception as exc:
        raise PluginParameterError(f"Plugin rejected parameter {name!r}: {exc}") from exc


def process_stereo(plugin: Any, audio: np.ndarray, sample_rate: float, *, block_frames: int = 4096) -> np.ndarray:
    """Process channel-first stereo float audio in bounded sequential blocks.

    Output has the same (2, frames) shape and dtype float32. A fresh output
    buffer is returned; input is never modified. Plugin state is preserved
    between blocks, and ``reset=False`` is used for each sequential call.
    """
    if not isinstance(audio, np.ndarray) or audio.ndim != 2 or audio.shape[0] != 2:
        raise VSTHostError("Audio must be a NumPy array shaped (2, frames)")
    if not np.issubdtype(audio.dtype, np.number) or not np.isfinite(audio).all():
        raise VSTHostError("Audio must contain only finite numeric samples")
    try:
        rate = float(sample_rate)
    except (TypeError, ValueError, OverflowError) as exc:
        raise VSTHostError("Sample rate must be a finite positive number") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise VSTHostError("Sample rate must be a finite positive number")
    if isinstance(block_frames, bool) or not isinstance(block_frames, int) or not 1 <= block_frames <= MAX_BLOCK_FRAMES:
        raise VSTHostError(f"block_frames must be an integer from 1 to {MAX_BLOCK_FRAMES}")

    frames = audio.shape[1]
    output = np.empty((2, frames), dtype=np.float32)
    for start in range(0, frames, block_frames):
        end = min(start + block_frames, frames)
        block = np.ascontiguousarray(audio[:, start:end], dtype=np.float32)
        try:
            rendered = np.asarray(plugin.process(block, sample_rate=rate, reset=False))
        except Exception as exc:
            raise VSTHostError(f"VST3 processing failed at frame {start}: {exc}") from exc
        if rendered.shape != block.shape:
            raise VSTHostError(
                f"Stereo effect returned shape {rendered.shape}; expected {block.shape}"
            )
        if not np.isfinite(rendered).all():
            raise VSTHostError(f"VST3 plugin returned non-finite audio at frame {start}")
        output[:, start:end] = rendered
    return output


def editor_support() -> dict[str, Any]:
    """Describe the deliberate GUI boundary; no editor is shown or emulated."""
    return {
        "supported": False,
        "reason": "This offline host exposes processing and parameters only; plugin GUI/editor embedding is unsupported.",
    }


def register_routes(app) -> None:
    """Register opt-in VST endpoints; loading occurs only on explicit path requests."""
    import base64
    import binascii

    from flask import jsonify, request

    @app.get("/vst/discover")
    def vst_discover_route():
        # Inventory paths only. No plugin code is loaded during discovery.
        return jsonify({"plugins": discover_vst3(), "metadata_loaded": False,
                        "editor": editor_support()})

    @app.post("/vst/inspect")
    def vst_inspect_route():
        body = request.get_json(force=True, silent=True) or {}
        try:
            bundle = _validated_bundle(body.get("path", ""))
            plugin = open_effect(bundle)
            return jsonify({"ok": True, "path": str(bundle),
                            "parameters": enumerate_parameters(plugin),
                            "editor": editor_support()})
        except (KeyError, TypeError, ValueError, VSTHostError) as exc:
            return jsonify({"error": "BAD_REQUEST", "detail": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "VST_PLUGIN_ERROR", "detail": f"{type(exc).__name__}: {exc}"}), 422

    @app.post("/vst/process")
    def vst_process_route():
        body = request.get_json(force=True, silent=True) or {}
        try:
            # Path is mandatory per request. The service does not cache or
            # implicitly select plugin instances across requests.
            plugin = open_effect(body.get("path", ""))
            parameters = body.get("parameters", {})
            if not isinstance(parameters, dict):
                raise VSTHostError("parameters must be an object mapping names to numbers")
            for name, value in parameters.items():
                set_parameter(plugin, name, value)
            if not isinstance(body.get("sample_rate"), (int, float)):
                raise VSTHostError("sample_rate is required and must be numeric")
            try:
                audio = np.frombuffer(base64.b64decode(body["audio_base64"], validate=True), dtype="<f4")
            except (KeyError, binascii.Error, ValueError) as exc:
                raise VSTHostError("audio_base64 must encode interleaved little-endian float32 stereo") from exc
            if audio.size % 2:
                raise VSTHostError("audio_base64 must contain an even number of float32 samples")
            stereo = audio.reshape(-1, 2).T
            rendered = process_stereo(plugin, stereo, body["sample_rate"],
                                      block_frames=body.get("block_frames", 4096))
            result_b64 = base64.b64encode(np.ascontiguousarray(rendered.T, dtype="<f4").tobytes()).decode("ascii")
            return jsonify({"ok": True, "audio_base64": result_b64,
                            "sample_rate": float(body["sample_rate"]),
                            "frames": int(rendered.shape[1]), "channels": 2})
        except (KeyError, TypeError, ValueError, VSTHostError) as exc:
            return jsonify({"error": "BAD_REQUEST", "detail": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "VST_PLUGIN_ERROR", "detail": f"{type(exc).__name__}: {exc}"}), 422
