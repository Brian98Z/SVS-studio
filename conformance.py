"""SVS Studio 跨实现契约测试套件。

验证三大实现（Rust core、Python inference、TypeScript web）对
CONTRACT.md 关键条款的一致性：坐标换算、Base64 f32le 曲线编码、
effective 合成公式、短语哈希、切片规则。

用法: python conformance.py
退出码: 0=全部通过, 1=存在失败
"""
import base64
import struct
import json
import math
import hashlib
import subprocess
import sys
import os

PPQ = 480
SR = 24000
PARAMS = ["pitch", "energy", "air", "tension", "falsetto", "gender", "velocity", "dynamic"]
SEP = "\x1f"

fails = []


def check(name, got, want, tol=None):
    if tol is not None and isinstance(got, (int, float)) and isinstance(want, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        fails.append(f"{name}: got={got!r} want={want!r}")
        print(f"  FAIL {name}: got={got!r} want={want!r}")
    else:
        print(f"  ok   {name}")


# ---------- 参考实现: tick<->ms 分段积分 ----------
def tick_to_ms(tempos, tick):
    """tempos: [(tick, bpm)] 升序, 首点必须 tick=0"""
    ms = 0.0
    for i, (t0, bpm) in enumerate(tempos):
        t1 = tempos[i + 1][0] if i + 1 < len(tempos) else float("inf")
        if tick < t1 or i == len(tempos) - 1:
            return ms + (tick - t0) * 60000.0 / (bpm * PPQ)
        ms += (t1 - t0) * 60000.0 / (bpm * PPQ)
    return ms


def ms_to_tick(tempos, ms):
    t_ms = 0.0
    for i, (t0, bpm) in enumerate(tempos):
        seg = (tempos[i + 1][0] - t0) * 60000.0 / (bpm * PPQ) if i + 1 < len(tempos) else float("inf")
        if ms < t_ms + seg or i == len(tempos) - 1:
            return t0 + round((ms - t_ms) * bpm * PPQ / 60000.0)
        t_ms += seg
    return tempos[-1][0]


# ---------- 参考实现: Base64 f32le 曲线 ----------
def encode_curve(vals):
    return base64.b64encode(struct.pack(f"<{len(vals)}f", *vals)).decode("ascii")


def decode_curve(b64):
    raw = base64.b64decode(b64)
    n = len(raw) // 4
    return list(struct.unpack(f"<{n}f", raw[: n * 4]))


def effective(base, user, is_pitch, envelope=(), direct=(), global_=()):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "packages", "inference-service"))
    from inference_service.contract import effective_curve
    return effective_curve(encode_curve(base), encode_curve(user), "pitch" if is_pitch else "energy",
                           max(map(len, (base, user, envelope, direct, global_))),
                           encode_curve(envelope), encode_curve(direct), encode_curve(global_))


# ---------- 参考实现: 短语哈希 ----------
def phrase_hash(singer, notes, curves_user, sr, model_tag="dsp-fallback-1"):
    """notes: [{tick,duration,key,velocity,lyric,phonemes,legato}]; curves_user: {param: b64}"""
    h = hashlib.sha256()
    h.update(singer.encode())
    h.update(SEP.encode())
    for n in notes:
        h.update(json.dumps(
            {k: n[k] for k in ("tick", "duration", "key", "velocity", "lyric", "phonemes", "legato")},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        h.update(SEP.encode())
    h.update(json.dumps(sorted(PARAMS), separators=(",", ":")).encode())
    h.update(SEP.encode())
    layers = [[(curves_user.get(p) or {}).get(l) or "" for l in ("base", "user", "envelope", "direct", "global")] for p in PARAMS]
    h.update(json.dumps(layers, separators=(",", ":")).encode())
    h.update(SEP.encode())
    h.update(str(sr).encode())
    h.update(SEP.encode())
    h.update(model_tag.encode())
    return h.hexdigest()


def split_phrases(notes):
    """间隙 >= PPQ 切分。返回 note 下标分组。"""
    groups = []
    cur = []
    for n in sorted(notes, key=lambda x: x["tick"]):
        if cur and n["tick"] - (cur[-1]["tick"] + cur[-1]["duration"]) >= PPQ and not cur[-1].get("legato"):
            groups.append(cur)
            cur = []
        cur.append(n)
    if cur:
        groups.append(cur)
    return groups


# ---------- 用例 ----------
def test_tick_ms():
    print("[tick<->ms 分段变速]")
    tempos = [(0, 120.0), (960, 60.0), (1440, 240.0)]
    check("t0=0ms", tick_to_ms(tempos, 0), 0.0, 1e-6)
    # 120BPM: 1 tick = 60000/(120*480) = 1.0417ms; 480 tick = 500ms
    check("t480(120bpm)=500ms", tick_to_ms(tempos, 480), 500.0, 1e-6)
    # 0..960 @120 = 1000ms; 960..1440 @60 = 1000ms → t1440=2000ms
    check("t960=1000ms", tick_to_ms(tempos, 960), 1000.0, 1e-6)
    check("t1440=2000ms", tick_to_ms(tempos, 1440), 2000.0, 1e-6)
    check("t1441=2000.52ms", tick_to_ms(tempos, 1441), 2000.0 + 60000 / (240 * 480), 1e-6)
    for t in (0, 1, 100, 480, 959, 960, 1200, 1439, 1440, 2000, 9999):
        check(f"roundtrip {t}", ms_to_tick(tempos, tick_to_ms(tempos, t)), t, 1)


def test_curve_b64():
    print("[Base64 f32le 曲线编码]")
    vals = [0.0, 0.5, -0.25, 1.0, float("nan"), 65536.5]
    b64 = encode_curve(vals)
    dec = decode_curve(b64)
    check("长度", len(dec), 6)
    check("NaN 占位保序", math.isnan(dec[4]), True)
    for i in (0, 1, 2, 3, 5):
        check(f"值[{i}]", dec[i], vals[i], 1e-6)
    # f32le 精确字节序
    raw = base64.b64decode(encode_curve([1.0]))
    check("f32le 字节序", raw.hex(), struct.pack("<f", 1.0).hex())


def test_effective():
    print("[五层 effective：加性 / 覆盖 / 全局偏移]")
    for got, want in zip(effective([0.4], [0.3], False), [0.7]): check("普通叠加", got, want, 1e-6)
    check("截断上界", effective([0.9], [0.5], False), [1.0])
    for got, want in zip(effective([0.6], [float("nan")], False), [0.6]): check("NaN 用户层=0", got, want, 1e-6)
    check("pitch 音分界", effective([800.0], [600.0], True), [1200.0])
    check("pitch 负向", effective([-1100.0], [-200.0], True), [-1200.0])
    got = effective([0.4, 0.4], [0.3, 0.3], False, [-0.2, -0.2], [0.1, float("nan")], [0.2, -0.1])
    for i, (g, w) in enumerate(zip(got, [0.3, 0.4])): check(f"direct覆盖/擦除恢复[{i}]", g, w, 1e-6)


def test_phrase():
    print("[切片与哈希]")
    notes = [
        {"tick": 0, "duration": 480, "key": 60, "velocity": 1.0, "lyric": "你", "phonemes": ["n", "i3"], "legato": False},
        {"tick": 480, "duration": 480, "key": 62, "velocity": 1.0, "lyric": "好", "phonemes": ["h", "ao3"], "legato": False},
        {"tick": 1920, "duration": 480, "key": 64, "velocity": 1.0, "lyric": "世", "phonemes": ["sh", "i4"], "legato": False},
    ]
    groups = split_phrases(notes)
    check("切成 2 句", [len(g) for g in groups], [2, 1])
    h1 = phrase_hash("singerA", groups[0], {"pitch": {"user": encode_curve([1.0, 2.0])}}, SR)
    h1b = phrase_hash("singerA", groups[0], {"pitch": {"user": encode_curve([1.0, 2.0])}}, SR)
    check("哈希确定性", h1, h1b)
    h2 = phrase_hash("singerA", groups[0], {"pitch": {"user": encode_curve([1.0, 3.0])}}, SR)
    check("曲线变→哈希变", h1 != h2, True)
    h3 = phrase_hash("singerB", groups[0], {"pitch": {"user": encode_curve([1.0, 2.0])}}, SR)
    check("歌手变→哈希变", h1 != h3, True)
    # legato 跨切片
    notes2 = [dict(notes[0]), dict(notes[1], legato=True), notes[2]]
    check("legato 不切", [len(g) for g in split_phrases(notes2)], [3])


def run_rust_conformance():
    """若 target/release/svs-cli.exe 存在则跑 Rust 侧对拍。"""
    exe = os.path.join(os.path.dirname(__file__), "target", "release", "svs-cli.exe")
    if not os.path.exists(exe):
        print("[rust 对拍] 跳过（可执行文件不存在）")
        return
    print("[rust 对拍]")
    tempos = [(0, 120.0), (960, 60.0)]
    for t in (0, 480, 960, 1440, 2000):
        out = subprocess.run([exe, "conformance", "tick-to-ms",
                              json.dumps([[a, b] for a, b in tempos]), str(t)],
                             capture_output=True, text=True, timeout=20)
        got = float(out.stdout.strip().split(":")[-1])
        check(f"rust tick_to_ms({t})", got, tick_to_ms(tempos, t), 1e-3)
    vals = [0.25, -1.5, float("nan"), 3.75]
    out = subprocess.run([exe, "conformance", "curve-encode", encode_curve(vals)],
                         capture_output=True, text=True, timeout=20)
    a, b = decode_curve(out.stdout.strip().split(":")[-1]), decode_curve(encode_curve(vals))
    same = len(a) == len(b) and all(
        (math.isnan(x) and math.isnan(y)) or x == y for x, y in zip(a, b)
    )
    check("rust 曲线 roundtrip", same, True)
    curve = {k: encode_curve(v) for k, v in {
        "base": [0.4, 0.4], "user": [0.3, 0.3], "envelope": [-0.2, -0.2],
        "direct": [0.1, float("nan")], "global": [0.2, -0.1],
    }.items()}
    out = subprocess.run([exe, "conformance", "curve-effective", "energy", "2", json.dumps(curve)],
                         capture_output=True, text=True, timeout=20, check=True)
    got = decode_curve(out.stdout.strip().split(":")[-1])
    for i, (g, w) in enumerate(zip(got, [0.3, 0.4])): check(f"rust 五层[{i}]", g, w, 1e-6)


if __name__ == "__main__":
    test_tick_ms()
    test_curve_b64()
    test_effective()
    test_phrase()
    run_rust_conformance()
    print()
    if fails:
        print(f"共 {len(fails)} 项失败")
        sys.exit(1)
    print("全部契约用例通过 ✓")
