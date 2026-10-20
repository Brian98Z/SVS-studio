"""Optional offline VST3 hosting with isolated native plugin execution.

Discovery is filesystem-only. Native plug-in code runs only in a spawned worker
for an explicit inspect or process request; GUI embedding is unsupported.
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


class VSTWorkerError(VSTHostError):
    """Raised when native plugin work crashes or times out in its worker."""

import base64
import binascii
import struct


MAX_PRESET_STATE_BYTES = 4 * 1024 * 1024
VST3_PRESET_MAGIC = b"VST3"

def _validate_preset_chunks(raw: bytes) -> None:
    if len(raw) < 56 or struct.unpack_from("<I", raw, 4)[0] != 1:
        raise VSTHostError("state_base64 must encode a version 1 VST3 preset")
    list_offset = struct.unpack_from("<Q", raw, 40)[0]
    if list_offset < 48 or list_offset + 8 > len(raw) or raw[list_offset:list_offset + 4] != b"List":
        raise VSTHostError("VST3 preset has an invalid chunk list")
    count = struct.unpack_from("<I", raw, list_offset + 4)[0]
    if count > (len(raw) - list_offset - 8) // 20:
        raise VSTHostError("VST3 preset chunk list is truncated")
    cursor = list_offset + 8
    for _ in range(count):
        if cursor + 20 > len(raw):
            raise VSTHostError("VST3 preset chunk list is truncated")
        chunk_offset = struct.unpack_from("<Q", raw, cursor + 4)[0]
        chunk_size = struct.unpack_from("<Q", raw, cursor + 12)[0]
        if chunk_offset > len(raw) or chunk_size > len(raw) - chunk_offset:
            raise VSTHostError("VST3 preset chunk is outside the preset data")
        cursor += 20


def validate_state(state: Any) -> str | None:
    """Validate and retain optional base64-encoded native VST3 preset bytes."""
    if state is None:
        return None
    if not isinstance(state, str):
        raise VSTHostError("state_base64 must be a base64 string or null")
    if len(state) > 4 * ((MAX_PRESET_STATE_BYTES + 2) // 3):
        raise VSTHostError("VST3 preset state exceeds 4 MiB")
    try:
        raw = base64.b64decode(state, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VSTHostError("state_base64 must contain valid base64") from exc
    if len(raw) > MAX_PRESET_STATE_BYTES:
        raise VSTHostError("VST3 preset state exceeds 4 MiB")
    if len(raw) < 48 or raw[:4] != VST3_PRESET_MAGIC:
        raise VSTHostError("state_base64 must encode a valid VST3 preset")
    _validate_preset_chunks(raw)
    return state


def _preset_data(plugin: Any) -> bytes:
    try:
        value = plugin.preset_data
    except (AttributeError, NotImplementedError) as exc:
        raise VSTHostError("Native VST3 preset state is not supported by this plugin host") from exc
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise VSTHostError("Native VST3 preset state API returned unsupported data")
    return bytes(value)


def restore_plugin_state(plugin: Any, state: str | None) -> None:
    """Restore VST3 preset bytes before parameter overrides, if present."""
    if state is None:
        return
    raw = base64.b64decode(validate_state(state), validate=True)
    current = _preset_data(plugin)
    if len(current) < 48 or current[:4] != VST3_PRESET_MAGIC:
        raise VSTHostError("Plugin did not expose a valid VST3 preset for class identification")
    if raw[8:40] != current[8:40]:
        raise VSTHostError("VST3 preset class ID does not match the selected plugin")
    try:
        plugin.preset_data = raw
    except (AttributeError, NotImplementedError) as exc:
        raise VSTHostError("Native VST3 preset state is not supported by this plugin host") from exc


def capture_plugin_state(plugin: Any) -> str:
    """Capture a bounded, validated native VST3 preset as base64."""
    raw = _preset_data(plugin)
    if len(raw) > MAX_PRESET_STATE_BYTES:
        raise VSTHostError("VST3 preset state exceeds 4 MiB")
    if len(raw) < 48 or raw[:4] != VST3_PRESET_MAGIC:
        raise VSTHostError("Plugin did not expose valid VST3 preset data")
    encoded = base64.b64encode(raw).decode("ascii")
    return validate_state(encoded)

MAX_BLOCK_FRAMES = 65536

def plugin_directory() -> Path:
    project = Path(os.environ.get("SVS_PROJECT_ROOT", Path(__file__).resolve().parents[3]))
    return Path(os.environ.get("SVS_PLUGIN_DIR", project / "plugins")).expanduser().resolve()


def resolve_plugin_path(path: str | os.PathLike[str]) -> Path:
    if not isinstance(path, (str, os.PathLike)) or not os.fspath(path).strip():
        raise VSTHostError("A VST3 plugin path is required")
    supplied = Path(path).expanduser()
    if supplied.is_absolute():
        return supplied.resolve()
    parts = supplied.parts
    if parts and parts[0].lower() == "plugins":
        supplied = Path(*parts[1:])
    root = plugin_directory()
    resolved = (root / supplied).resolve()
    if not resolved.is_relative_to(root):
        raise VSTHostError("Relative plugin path must stay inside the project plugins directory")
    return resolved


def _pedalboard():
    try:
        import pedalboard
    except ImportError as exc:
        raise VSTRuntimeUnavailable(
            "VST3 hosting requires the optional 'pedalboard' dependency; "
            "install from repository-root requirements-vst.txt"
        ) from exc
    return pedalboard


def _validated_bundle(path: str | os.PathLike[str]) -> Path:
    bundle = resolve_plugin_path(path).resolve(strict=True)
    if bundle.suffix.lower() != ".vst3" or not (bundle.is_dir() or bundle.is_file()):
        raise VSTHostError("Plugin path must identify an existing .vst3 binary or bundle directory")
    if bundle.is_dir():
        try:
            next(p for p in bundle.rglob("*") if p.is_file())
        except StopIteration as exc:
            raise VSTHostError("VST3 bundle is empty") from exc
    return bundle


def discover_vst3(search_roots: Iterable[str | os.PathLike[str]] | None = None) -> list[dict[str, str]]:
    """Recursively list VST3 files and bundles without loading plugin code."""
    if search_roots is None:
        roots = [plugin_directory()]
    else:
        roots = [Path(p).expanduser() for p in search_roots]

    found: dict[str, dict[str, str]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.suffix.lower() != ".vst3":
                    if entry.is_dir():
                        stack.append(entry)
                    continue
                if not (entry.is_dir() or entry.is_file()):
                    continue
                resolved = entry.resolve()
                if resolved.suffix.lower() != ".vst3" or not (resolved.is_dir() or resolved.is_file()):
                    continue
                try:
                    display_path = "plugins/" + resolved.relative_to(plugin_directory()).as_posix()
                except ValueError:
                    display_path = str(resolved)
                found[str(resolved)] = {"name": entry.name, "path": display_path}
    return [found[key] for key in sorted(found, key=str.casefold)]


def open_effect(path: str | os.PathLike[str]):
    """Instantiate an explicitly selected VST3 path (worker-only operation)."""
    bundle = _validated_bundle(path)
    if os.name == "nt" and bundle.is_dir():
        binary = bundle / "Contents" / "x86_64-win" / f"{bundle.stem}.vst3"
        if not binary.is_file():
            raise VSTHostError(f"VST3 bundle has no Windows x86_64 binary: {bundle}")
        bundle = binary
    runtime = _pedalboard()
    try:
        return runtime.load_plugin(str(bundle))
    except Exception as exc:
        raise VSTHostError(f"Could not instantiate VST3 plugin at {bundle}: {exc}") from exc


def enumerate_parameters(plugin: Any) -> list[dict[str, Any]]:
    """Return Pedalboard parameter values and supported metadata."""
    try:
        parameters = plugin.parameters
    except Exception as exc:
        raise VSTHostError(f"Plugin parameter enumeration failed: {exc}") from exc
    result = []
    for name, parameter in parameters.items():
        item: dict[str, Any] = {"name": str(name)}
        try:
            value = getattr(plugin, name)
        except Exception:
            value = None
        if value is not None:
            try:
                declared_type = getattr(parameter, "type", None)
                if declared_type is bool or isinstance(value, (bool, np.bool_)):
                    item["value"] = bool(value)
                elif declared_type is str or isinstance(value, str):
                    item["value"] = str(value)
                else:
                    item["value"] = float(value)
            except (TypeError, ValueError, OverflowError):
                pass
        for key in ("min_value", "max_value"):
            candidate = getattr(parameter, key, None)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and math.isfinite(float(candidate)):
                item[key] = float(candidate)
        steps = getattr(parameter, "steps", None)
        if isinstance(steps, int) and not isinstance(steps, bool) and steps > 1:
            item["steps"] = int(steps)
            if "min_value" in item and "max_value" in item:
                item["step"] = float((item["max_value"] - item["min_value"]) / (steps - 1))
        explicit_step = getattr(parameter, "step", None)
        if isinstance(explicit_step, (int, float)) and not isinstance(explicit_step, bool) and math.isfinite(float(explicit_step)):
            item["step"] = float(explicit_step)
        try:
            parameter_type = getattr(parameter, "type", None)
        except Exception:
            parameter_type = None
        if isinstance(parameter_type, type):
            item["type"] = {str: "string", bool: "boolean", int: "number", float: "number"}.get(parameter_type, "string")
        elif isinstance(parameter_type, bool):
            item["type"] = "boolean"
        elif isinstance(parameter_type, (str, int, float)):
            item["type"] = "string" if isinstance(parameter_type, str) else "number"
        try:
            valid_values = getattr(parameter, "valid_values", None)
        except Exception:
            valid_values = None
        if isinstance(valid_values, (list, tuple, set, frozenset)):
            values = []
            for candidate in valid_values:
                try:
                    if isinstance(candidate, bool):
                        values.append(bool(candidate))
                    elif isinstance(candidate, str):
                        values.append(str(candidate))
                except (TypeError, ValueError, OverflowError):
                    continue
            if values:
                item["valid_values"] = values
        if "type" not in item:
            if getattr(parameter, "is_boolean", False) or isinstance(item.get("value"), bool):
                item["type"] = "boolean"
            elif isinstance(item.get("value"), str):
                item["type"] = "string"
            else:
                item["type"] = "number"
        result.append(item)
    return result


def set_parameter(plugin: Any, name: str, value: Any) -> Any:
    """Set a named parameter and return its actual plugin readback."""
    if not isinstance(name, str) or not name:
        raise PluginParameterError("Parameter name must be a non-empty string")
    try:
        parameter = plugin.parameters.get(name)
    except Exception as exc:
        raise VSTHostError(f"Plugin parameter enumeration failed: {exc}") from exc
    if parameter is None:
        raise PluginParameterError(f"Plugin has no parameter named {name!r}")
    if isinstance(value, bool):
        candidate: Any = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        candidate = float(value)
        if not math.isfinite(candidate):
            raise PluginParameterError("Parameter value must be finite")
        low, high = getattr(parameter, "min_value", None), getattr(parameter, "max_value", None)
        if low is not None and candidate < low or high is not None and candidate > high:
            raise PluginParameterError(f"Value {candidate} outside parameter range [{low}, {high}]")
    elif isinstance(value, str):
        valid = getattr(parameter, "valid_values", None)
        if valid is not None and value not in valid:
            raise PluginParameterError(f"Value {value!r} is not valid for parameter {name!r}")
        candidate = value
    else:
        raise PluginParameterError("Parameter value must be numeric, boolean, or string")
    try:
        setattr(plugin, name, candidate)
        actual = getattr(plugin, name)
    except Exception as exc:
        raise PluginParameterError(f"Plugin rejected parameter {name!r}: {exc}") from exc
    try:
        if isinstance(candidate, bool):
            return bool(actual)
        if isinstance(candidate, str):
            return str(actual)
        numeric = float(actual)
        if not math.isfinite(numeric):
            raise PluginParameterError(f"Plugin returned non-finite value for parameter {name!r}")
        return numeric
    except (TypeError, ValueError, OverflowError) as exc:
        raise PluginParameterError(f"Plugin returned invalid value for parameter {name!r}") from exc

def _validate_parameters(parameters: Any) -> dict[str, Any]:
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise VSTHostError("parameters must be an object mapping names to scalar values")
    for name, value in parameters.items():
        if not isinstance(name, str) or not name:
            raise PluginParameterError("Parameter names must be non-empty strings")
        if isinstance(value, bool) or isinstance(value, str):
            continue
        if isinstance(value, (int, float)):
            try:
                finite = math.isfinite(float(value))
            except (OverflowError, ValueError):
                finite = False
            if finite:
                continue
        raise PluginParameterError(f"Parameter {name!r} must be finite numeric, boolean, or string")
    return parameters

def _run_worker(operation: str, payload: dict[str, Any], *, timeout: float = 60.0):
    from .vst_worker import run_worker
    try:
        result = run_worker(operation, payload, timeout=timeout)
        if operation == "process":
            shape, dtype, data = result
            return np.frombuffer(data, dtype=np.dtype(dtype)).reshape(shape).copy()
        return result
    except TimeoutError as exc:
        raise VSTWorkerError(str(exc)) from exc
    except RuntimeError as exc:
        raise VSTWorkerError(f"VST worker failed: {exc}") from exc


def inspect_effect(path: str | os.PathLike[str], *, timeout: float = 60.0) -> list[dict[str, Any]]:
    """Load and inspect an explicitly selected plugin in a spawned worker."""
    plugin_path = _validated_bundle(path)
    return _run_worker("inspect", {"path": str(plugin_path)}, timeout=timeout)


def process_effect(path: str | os.PathLike[str], audio: np.ndarray, sample_rate: float, *,
                   parameters: dict[str, Any] | None = None, state_base64: str | None = None,
                   block_frames: int = 4096, tail_seconds: float = 0.0,
                   timeout: float = 60.0) -> np.ndarray:
    """Process stereo float audio in an isolated worker, optionally rendering bounded tail silence."""
    plugin_path = _validated_bundle(path)
    values = _validate_parameters(parameters)
    state = validate_state(state_base64)
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
    if rate > 768000:
        raise VSTHostError("Sample rate must not exceed 768000 Hz")
    if isinstance(block_frames, bool) or not isinstance(block_frames, int) or not 1 <= block_frames <= MAX_BLOCK_FRAMES:
        raise VSTHostError(f"block_frames must be an integer from 1 to {MAX_BLOCK_FRAMES}")
    if isinstance(tail_seconds, bool) or not isinstance(tail_seconds, (int, float)) or not math.isfinite(float(tail_seconds)) or not 0 <= tail_seconds <= 10:
        raise VSTHostError("tail_seconds must be a finite number from 0 to 10")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0:
        raise VSTHostError("timeout must be a finite positive number")
    source = np.ascontiguousarray(audio, dtype=np.float32)
    rendered = _run_worker("process", {"path": str(plugin_path), "audio": source,
        "sample_rate": rate, "parameters": values, "state_base64": state,
        "block_frames": block_frames, "tail_seconds": float(tail_seconds)}, timeout=timeout)
    return np.asarray(rendered, dtype=np.float32)

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
    if rate > 768000:
        raise VSTHostError("Sample rate must not exceed 768000 Hz")
    if isinstance(block_frames, bool) or not isinstance(block_frames, int) or not 1 <= block_frames <= MAX_BLOCK_FRAMES:
        raise VSTHostError(f"block_frames must be an integer from 1 to {MAX_BLOCK_FRAMES}")

    frames = audio.shape[1]
    output = np.empty((2, frames), dtype=np.float32)
    if frames == 0:
        return output
    chunks: list[np.ndarray] = []
    produced = 0
    withheld = 0
    latency = None
    skip = 0
    drain_frames = int(rate * 10)
    pending = np.ascontiguousarray(audio, dtype=np.float32)
    while produced < frames + skip:
        try:
            rendered = np.asarray(plugin.process(pending, sample_rate=rate, reset=False,
                                                buffer_size=block_frames))
        except TypeError:
            try:
                rendered = np.asarray(plugin.process(pending, sample_rate=rate, reset=False))
            except Exception as exc:
                raise VSTHostError(f"VST3 processing failed: {exc}") from exc
        except Exception as exc:
            raise VSTHostError(f"VST3 processing failed: {exc}") from exc
        if rendered.ndim != 2 or rendered.shape[0] != 2 or rendered.shape[1] > pending.shape[1]:
            raise VSTHostError(f"Stereo effect returned shape {rendered.shape}; expected at most {pending.shape[1]} frames")
        if not np.isfinite(rendered).all():
            raise VSTHostError("VST3 plugin returned non-finite audio")
        reported = getattr(plugin, "reported_latency_samples", 0)
        if isinstance(reported, bool) or not isinstance(reported, int) or not 0 <= reported <= int(rate * 10):
            raise VSTHostError("VST3 reported latency must be an integer within 10 seconds")
        if latency is not None and reported != latency:
            raise VSTHostError("VST3 latency changed during offline rendering; restart with fixed plugin settings")
        latency = reported
        # Pedalboard may already omit buffered startup samples from its return.
        # Remove only the remaining reported delay, never compensate twice.
        withheld += pending.shape[1] - rendered.shape[1]
        skip = max(0, latency - withheld)
        if rendered.shape[1]:
            chunks.append(np.asarray(rendered, dtype=np.float32))
            produced += rendered.shape[1]
        if produced >= frames + skip:
            break
        if drain_frames <= 0:
            raise VSTHostError(f"VST3 plugin latency exceeds the 10-second drain limit ({produced}/{frames} output frames)")
        count = min(block_frames, drain_frames)
        pending = np.zeros((2, count), dtype=np.float32)
        drain_frames -= count
    joined = np.concatenate(chunks, axis=1) if len(chunks) > 1 else chunks[0]
    output[:] = joined[:, skip:skip + frames]
    return output


def editor_support() -> dict[str, Any]:
    """Report standalone native editor support, not embedded/live-instance editing."""
    return {
        "supported": True,
        "mode": "isolated_native_window",
        "reason": "Standalone editor for the tool instance; native preset state and parameter readback after close. No embedding or live-track linkage.",
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
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "BAD_REQUEST", "detail": "Request body must be a JSON object"}), 400
        try:
            plugin_path = _validated_bundle(body.get("path", ""))
            return jsonify({"ok": True, "path": str(plugin_path),
                            "parameters": inspect_effect(plugin_path), "editor": editor_support()})
        except VSTWorkerError as exc:
            return jsonify({"error": "VST_PLUGIN_ERROR", "detail": str(exc)}), 422
        except (KeyError, TypeError, ValueError, VSTHostError) as exc:
            return jsonify({"error": "BAD_REQUEST", "detail": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "VST_PLUGIN_ERROR", "detail": f"{type(exc).__name__}: {exc}"}), 422

    @app.post("/vst/process")
    def vst_process_route():
        body = request.get_json(force=True, silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "BAD_REQUEST", "detail": "Request body must be a JSON object"}), 400
        try:
            plugin_path = _validated_bundle(body.get("path", ""))
            parameters = _validate_parameters(body.get("parameters", {}))
            state_base64 = validate_state(body.get("state_base64"))
            sample_rate = body.get("sample_rate")
            if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float)):
                raise VSTHostError("sample_rate is required and must be numeric")
            try:
                audio = np.frombuffer(base64.b64decode(body["audio_base64"], validate=True), dtype="<f4")
            except (KeyError, binascii.Error, ValueError) as exc:
                raise VSTHostError("audio_base64 must encode interleaved little-endian float32 stereo") from exc
            if audio.size % 2:
                raise VSTHostError("audio_base64 must contain an even number of float32 samples")
            rendered = process_effect(plugin_path, audio.reshape(-1, 2).T, sample_rate,
                parameters=parameters, state_base64=state_base64,
                block_frames=body.get("block_frames", 4096), tail_seconds=body.get("tail_seconds", 0.0))
            result_b64 = base64.b64encode(np.ascontiguousarray(rendered.T, dtype="<f4").tobytes()).decode("ascii")
            return jsonify({"ok": True, "audio_base64": result_b64,
                            "sample_rate": float(sample_rate), "frames": int(rendered.shape[1]), "channels": 2})
        except VSTWorkerError as exc:
            return jsonify({"error": "VST_PLUGIN_ERROR", "detail": str(exc)}), 422
        except (KeyError, TypeError, ValueError, VSTHostError) as exc:
            return jsonify({"error": "BAD_REQUEST", "detail": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": "VST_PLUGIN_ERROR", "detail": f"{type(exc).__name__}: {exc}"}), 422
