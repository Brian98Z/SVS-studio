"""契约实现：坐标换算、曲线编解码、短语切片与哈希。

与 Rust svs-core / TypeScript web 端保持逐字节一致（见 docs/CONTRACT.md）。
"""
import base64
import hashlib
import json
import math
import struct

PPQ = 480
SR = 24000
PARAMS = ["pitch", "energy", "air", "tension", "falsetto", "gender", "velocity", "dynamic"]
SEP = "\x1f"
PHRASE_GAP_TICKS = PPQ
MODEL_TAG = "dsp-fallback-1"

# ---------------- 坐标 ----------------

def _tempo_curve(point):
    curve = point.get("curve", "step")
    if curve not in ("step", "linear"):
        raise ValueError("tempo curve must be 'step' or 'linear'")
    return curve


def validate_tempos(tempos):
    """Validate a tick-zero anchored, strictly increasing positive-BPM map."""
    if not tempos or tempos[0]["tick"] != 0:
        raise ValueError("tempo table must start at tick 0")
    for i, point in enumerate(tempos):
        bpm = point["bpm"]
        if not isinstance(bpm, (int, float)) or not math.isfinite(bpm) or bpm <= 0:
            raise ValueError("tempo BPM must be finite and positive")
        _tempo_curve(point)
        if i + 1 < len(tempos) and tempos[i + 1]["tick"] <= point["tick"]:
            raise ValueError("tempo ticks must be strictly increasing")


def _segment_ms(point, next_point, ticks):
    factor = 60000.0 / PPQ
    if next_point is None or _tempo_curve(point) != "linear":
        return ticks * factor / point["bpm"]
    span = next_point["tick"] - point["tick"]
    slope = (next_point["bpm"] - point["bpm"]) / span
    if abs(slope) < 1e-12:
        return ticks * factor / point["bpm"]
    return factor * math.log1p(slope * ticks / point["bpm"]) / slope


def tick_to_ms(tempos, tick):
    """Map tick to milliseconds by exact piecewise step/linear-BPM integration."""
    if not tempos:
        return 0.0
    ms = 0.0
    for i, point in enumerate(tempos):
        nxt = tempos[i + 1] if i + 1 < len(tempos) else None
        end = min(float(tick), float(nxt["tick"])) if nxt else float(tick)
        if end > point["tick"]:
            ms += _segment_ms(point, nxt, end - point["tick"])
        if nxt is None or tick < nxt["tick"]:
            return ms
    return ms


def ms_to_tick(tempos, ms):
    """Inverse of tick_to_ms; nearest tick, with half ticks rounded upward."""
    if not tempos or ms <= 0:
        return 0
    factor = 60000.0 / PPQ
    elapsed = 0.0
    for i, point in enumerate(tempos):
        nxt = tempos[i + 1] if i + 1 < len(tempos) else None
        span = nxt["tick"] - point["tick"] if nxt else None
        duration = _segment_ms(point, nxt, span) if span is not None else float("inf")
        if ms < elapsed + duration:
            remaining = ms - elapsed
            if nxt is not None and _tempo_curve(point) == "linear":
                slope = (nxt["bpm"] - point["bpm"]) / span
                ticks = (remaining * point["bpm"] / factor if abs(slope) < 1e-12
                         else point["bpm"] * math.expm1(remaining * slope / factor) / slope)
            else:
                ticks = remaining * point["bpm"] / factor
            return max(0, min(0xFFFFFFFF, int(math.floor(point["tick"] + ticks + 0.5))))
        elapsed += duration
    return tempos[-1]["tick"]


# ---------------- 曲线 ----------------

def encode_f32(vals):
    return base64.b64encode(struct.pack(f"<{len(vals)}f", *vals)).decode("ascii")


def decode_f32(b64):
    if not b64:
        return []
    raw = base64.b64decode(b64)
    n = len(raw) // 4
    return list(struct.unpack(f"<{n}f", raw[: n * 4]))


def param_range(param):
    return (-1200.0, 1200.0) if param == "pitch" else (0.0, 1.0)


def effective_curve(base_b64, user_b64, param, length, envelope_b64="", direct_b64="", global_b64=""):
    """Evaluate the five layers; NaN/non-finite samples are absent, not zero edits."""
    layers = [decode_f32(value) for value in
              (base_b64, user_b64, envelope_b64, direct_b64, global_b64)]
    base, user, envelope, direct, global_ = layers
    lo, hi = param_range(param)
    out = []
    for i in range(length):
        def sample(layer):
            value = layer[i] if i < len(layer) else float("nan")
            return value if math.isfinite(value) else None

        b, u, e, d, g = map(sample, layers)
        lower = (b or 0.0) + (u or 0.0) + (e or 0.0)
        value = d if d is not None else lower
        if g is not None:
            value += g
        out.append(max(lo, min(hi, value)) if math.isfinite(value) else 0.0)
    return out


def curve_cache_key(curves, tempos):
    """Stable cache material: layer order follows the shared five-layer contract."""
    payload = []
    for param in PARAMS:
        layers = (curves or {}).get(param) or {}
        if isinstance(layers, str):
            layers = {"user": layers}
        payload.append([layers.get(layer, "") for layer in
                        ("base", "user", "envelope", "direct", "global")])
    return json.dumps(payload, separators=(",", ":")) + ":" + json.dumps(
        tempos or [], separators=(",", ":"),
    )


# ---------------- 切片与哈希 ----------------

def split_phrases(notes):
    """间隙 ≥ 480 tick 切分；legato 音符不作为新句起点。"""
    ns = sorted(notes, key=lambda n: (n["tick"], n.get("key", 60)))
    groups, cur = [], []
    for n in ns:
        if cur:
            gap = n["tick"] - (cur[-1]["tick"] + cur[-1]["duration"])
            if gap >= PHRASE_GAP_TICKS and not n.get("legato"):
                groups.append(cur)
                cur = []
        cur.append(n)
    if cur:
        groups.append(cur)
    return groups


def _canon_note(n):
    return json.dumps(
        {k: n[k] for k in ("tick", "duration", "key", "velocity", "lyric", "phonemes", "legato")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )




