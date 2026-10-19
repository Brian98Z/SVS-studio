"""合成编排：PhraseSpec → 波形。

ONNX 模式：notes_spec 转帧级输入 → OnnxPipeline.synth。
DSP 模式：逐音符 dsp_fallback.synth_note → 拼接 + 交叉淡化 + 余弦窗。
"""
from __future__ import annotations

import numpy as np

from . import dsp_fallback
from .contract import SR, effective_curve, decode_f32, PARAMS, tick_to_ms

HOP = dsp_fallback.HOP


def _effective_arrays(curves: dict, length: int) -> dict[str, np.ndarray]:
    result = {}
    for param in PARAMS:
        layers = (curves or {}).get(param) or {}
        if not isinstance(layers, dict):
            layers = {}
        layers = [layers.get(name, "") for name in ("base", "user", "envelope", "direct", "global")]
        values = effective_curve(*layers[:2], param, length, *layers[2:])
        arr = np.asarray(values, dtype=np.float32)
        # DSP neutral defaults are restored only where every layer was absent.
        if len(arr):
            present = np.zeros(length, dtype=bool)
            for encoded in layers:
                decoded = decode_f32(encoded)
                count = min(length, len(decoded))
                if count:
                    present[:count] |= np.isfinite(decoded[:count])
            arr[~present] = np.nan
        result[param] = arr
    return result


def synth_phrase_dsp(spec: dict, sr: int = SR) -> np.ndarray:
    """Render notes whose ticks are phrase-relative; curves/tempo remain absolute."""
    from .contract import tick_to_ms

    tempos = spec.get("tempos") or [{"tick": 0, "bpm": 120.0}]
    notes = spec["notes"]
    if not notes:
        return np.zeros(0, dtype=np.float32)
    origin = int(spec.get("offset_ticks", 0))
    end_tick = max(n["tick"] + n["duration"] for n in notes)
    origin_ms = tick_to_ms(tempos, origin)
    end_ms = tick_to_ms(tempos, origin + end_tick) - origin_ms
    buf = np.zeros(int(end_ms / 1000 * sr) + dsp_fallback.FADE * 2, dtype=np.float32)
    curves = _effective_arrays(spec.get("curves") or {}, origin + end_tick + 1)
    timbre = spec.get("timbre") or "voice"

    for note in notes:
        absolute_start = origin + note["tick"]
        absolute_end = absolute_start + note["duration"]
        start_ms = tick_to_ms(tempos, absolute_start) - origin_ms
        duration_ms = tick_to_ms(tempos, absolute_end) - tick_to_ms(tempos, absolute_start)
        s0 = int(start_ms / 1000 * sr)
        dur = max(int(duration_ms / 1000 * sr), 256)
        window = {p: a[absolute_start:absolute_end] for p, a in curves.items()}

        def parameter(name):
            values = window[name]
            return values if values.size and np.any(np.isfinite(values)) else None

        velocity = parameter("velocity")
        vel_scalar = float(note.get("velocity", 1.0))
        pcm = dsp_fallback.synth_note(
            key=note["key"], dur_samples=dur, velocity=vel_scalar,
            phonemes=note.get("phonemes") or [],
            pitch_cents=parameter("pitch") if timbre == "voice" else None,
            energy=parameter("energy"),
            air=parameter("air") if timbre == "voice" else None,
            tension=parameter("tension") if timbre == "voice" else None,
            falsetto=parameter("falsetto") if timbre == "voice" else None,
            gender=parameter("gender") if timbre == "voice" else None,
            timbre=timbre,
        )
        # Velocity is a level control in this backend; dynamic is an independent
        # post-envelope level control. NaN holes retain the neutral multiplier.
        for name, neutral in (("velocity", vel_scalar), ("dynamic", 1.0)):
            vals = window[name]
            if vals.size and np.any(np.isfinite(vals)):
                idx = np.minimum((np.arange(len(pcm)) * len(vals) / len(pcm)).astype(int), len(vals) - 1)
                level = np.where(np.isfinite(vals[idx]), vals[idx], neutral).astype(np.float32)
                if name == "velocity":
                    level /= max(vel_scalar, 1e-6)
                pcm *= level
        pcm = dsp_fallback.dsp_fallback_apply_fades(pcm, 32) if timbre != "voice" else dsp_fallback.apply_fades(pcm)
        e = min(s0 + len(pcm), len(buf))
        xf = min(dsp_fallback.XFADE, len(pcm) // 4, max(e - s0 - len(pcm) + dsp_fallback.XFADE, 0))
        if xf > 0 and s0 > 0:
            ov = min(xf, e - s0, len(buf) - s0)
            t = np.sqrt(np.linspace(0, 1, ov, dtype=np.float32))
            buf[s0:s0 + ov] = buf[s0:s0 + ov] * (1 - t) + pcm[:ov] * t
            buf[s0 + ov:e] = pcm[ov:ov + (e - s0 - ov)]
        else:
            buf[s0:e] = pcm[:e - s0]
    peak = float(np.max(np.abs(buf))) if len(buf) else 0.0
    if peak > 0.99:
        buf *= 0.99 / peak
    return buf


def synth_phrase(spec: dict, mode: str, pipeline=None, cache_key: str | None = None) -> np.ndarray:
    if mode == "onnx" and pipeline is not None:
        notes = spec["notes"]
        if not notes:
            return np.zeros(0, dtype=np.float32)
        origin = int(spec.get("offset_ticks", 0))
        end_tick = max(n["tick"] + n["duration"] for n in notes)
        curves = _effective_arrays(spec.get("curves") or {}, origin + end_tick + 1)
        if any(np.any(np.isfinite(values)) for values in curves.values()):
            raise ValueError("ONNX adapter does not support external curve layers")
        tempos = spec.get("tempos") or [{"tick": 0, "bpm": 120.0}]
        frames_hint = max(int((tick_to_ms(tempos, origin + end_tick) - tick_to_ms(tempos, origin)) / 1000 * SR / HOP), 128)
        phones, note_div = [], []
        for note in notes:
            phonemes = note.get("phonemes") or ["a"]
            duration_ms = tick_to_ms(tempos, origin + note["tick"] + note["duration"]) - tick_to_ms(tempos, origin + note["tick"])
            frames = max(int(duration_ms / 1000 * SR / HOP), 1)
            phones.extend([_phone_id(phone) for phone in phonemes])
            note_div.extend([frames // len(phonemes)] * len(phonemes))
        return pipeline.synth({"phones": phones, "note_div": note_div}, np.empty(0, dtype=np.float32), frames_hint)
    return synth_phrase_dsp(spec)


_PHONE_IDS = {}
_next_id = [1]


def _phone_id(p: str) -> int:
    if p not in _PHONE_IDS:
        _PHONE_IDS[p] = _next_id[0]
        _next_id[0] += 1
    return _PHONE_IDS[p]
