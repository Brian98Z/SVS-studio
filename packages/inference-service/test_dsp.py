"""DSP 回退合成器测试：可听性前提是数值有效性。"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "inference-service"))
from inference_service import dsp_fallback as dsp  # noqa: E402
from inference_service import contract as C  # noqa: E402
from inference_service.pipeline import synth_phrase  # noqa: E402


def test_note_basics():
    pcm = dsp.synth_note(
        key=60, dur_samples=12000, velocity=1.0, phonemes=["n", "i3"],
        pitch_cents=None, energy=None, air=None, tension=None,
        falsetto=None, gender=None,
    )
    assert len(pcm) == 12000
    assert pcm.dtype == np.float32
    peak = float(np.max(np.abs(pcm)))
    assert 0.0001 < peak <= 1.0, f"peak={peak}"
    # 非静音
    assert float(np.sqrt(np.mean(pcm ** 2))) > 0.005


def test_pitch_curve_shifts_frequency():
    n = 12000
    flat = np.zeros(n, dtype=np.float32)
    up = np.full(n, 1200.0, dtype=np.float32)  # +1 八度
    a = dsp.synth_note(60, n, 1.0, ["a"], flat, None, None, None, None, None)
    b = dsp.synth_note(60, n, 1.0, ["a"], up, None, None, None, None, None)

    def fund(x):
        sp = np.abs(np.fft.rfft(x))
        f = np.fft.rfftfreq(len(x), 1 / dsp.SR)
        return float(f[int(np.argmax(sp))])

    # 主谱峰（含谐波结构中最高峰）频率至少上移一个八度的一半
    # a 的峰在 5×f0=1310，b 在 2×f0=1046 → 用最低显著峰判基频更稳
    def lowest_peak(x):
        sp = np.abs(np.fft.rfft(x))
        f = np.fft.rfftfreq(len(x), 1 / dsp.SR)
        idx = [i for i in range(2, len(sp) - 2)
               if sp[i] == sp[i - 2:i + 3].max() and sp[i] > sp.max() * 0.3]
        return float(f[idx[0]]) if idx else 0.0

    fa, fb = lowest_peak(a), lowest_peak(b)
    assert fb > fa * 1.8, (fa, fb)  # 262 → 524


def test_air_raises_noise():
    n = 12000
    flat = np.zeros(n, dtype=np.float32)
    dry = dsp.synth_note(60, n, 1.0, ["a"], flat, None, np.zeros(n, np.float32), None, None, None)
    airy = dsp.synth_note(60, n, 1.0, ["a"], flat, None, np.ones(n, np.float32), None, None, None)
    # 4kHz 以上能量占比：噪声平坦谱 vs 谐波低频集中
    def hf_ratio(x):
        sp = np.abs(np.fft.rfft(x))
        f = np.fft.rfftfreq(len(x), 1 / dsp.SR)
        return float(sp[f > 4000].sum() / (sp.sum() + 1e-9))
    assert hf_ratio(airy) > hf_ratio(dry) * 3, (hf_ratio(dry), hf_ratio(airy))


def test_fades_prevent_click():
    pcm = np.ones(8000, dtype=np.float32)
    out = dsp.apply_fades(pcm.copy())
    assert abs(float(out[0])) < 1e-6, "起点必须淡入到 0"
    assert abs(float(out[-1])) < 1e-6
    assert abs(float(out[4000]) - 1.0) < 1e-6


def test_phrase_end_to_end():
    spec = {
        "hash": "x", "singer": "builtin.fallback", "offset_ticks": 0,
        "tempos": [{"tick": 0, "bpm": 120.0}],
        "notes": [
            {"tick": 0, "duration": 480, "key": 60, "velocity": 1.0,
             "lyric": "你", "phonemes": ["n", "i3"], "legato": False},
            {"tick": 480, "duration": 480, "key": 62, "velocity": 1.0,
             "lyric": "好", "phonemes": ["h", "ao3"], "legato": False},
        ],
        "curves": {"pitch": C.encode_f32([0.0] * 960)},
    }
    pcm = synth_phrase(spec, "dsp")
    assert len(pcm) > 20000  # 2 音符 @120bpm = 1s = 24000 样本
    peak = float(np.max(np.abs(pcm)))
    assert peak <= 1.0
    assert float(np.sqrt(np.mean(pcm ** 2))) > 0.01


def test_contract_parity():
    """与 conformance.py 参考实现对拍。"""
    tempos = [{"tick": 0, "bpm": 120.0}, {"tick": 960, "bpm": 60.0}]
    assert abs(C.tick_to_ms(tempos, 960) - 1000.0) < 1e-6
    assert abs(C.tick_to_ms(tempos, 1440) - 2000.0) < 1e-6  # 960..1440 @60bpm = 1000ms
    assert abs(C.tick_to_ms(tempos, 1441) - 2000.0 - (60000 / (60 * 480))) < 1e-6
    vals = [0.5, float("nan"), -0.25]
    dec = C.decode_f32(C.encode_f32(vals))
    notes = [
        {"tick": 0, "duration": 480, "key": 60, "velocity": 1.0, "lyric": "a", "phonemes": [], "legato": False},
        {"tick": 480, "duration": 480, "key": 62, "velocity": 1.0, "lyric": "b", "phonemes": [], "legato": False},
        {"tick": 1920, "duration": 480, "key": 64, "velocity": 1.0, "lyric": "c", "phonemes": [], "legato": False},
    ]
    groups = C.split_phrases(notes)
    assert [len(g) for g in groups] == [2, 1]

def test_tempo_change_and_note_velocity_are_audible():
    note = {"tick": 0, "duration": 960, "key": 60, "velocity": 1.0,
            "lyric": "a", "phonemes": ["a"], "legato": False}
    fast = [{"tick": 0, "bpm": 120.0}]
    changed = [{"tick": 0, "bpm": 120.0}, {"tick": 480, "bpm": 60.0}]
    a = synth_phrase({"notes": [note], "tempos": fast}, "dsp")
    b = synth_phrase({"notes": [note], "tempos": changed}, "dsp")
    assert len(b) > len(a) * 1.4
    values = [0.0] * 960
    quieter = synth_phrase({"notes": [note], "tempos": fast,
                             "curves": {"velocity": {"direct": C.encode_f32(values)}}}, "dsp")
    assert float(np.max(np.abs(quieter))) < float(np.max(np.abs(a))) * 0.02


def test_five_layer_precedence_holes_and_erase():
    nan = float("nan")
    values = C.effective_curve(
        C.encode_f32([0.2, 0.1, nan, nan]),
        C.encode_f32([0.1, 0.2, 0.3, nan]), "energy", 4,
        C.encode_f32([0.1, 0.1, nan, nan]),
        C.encode_f32([0.8, nan, nan, nan]),
        C.encode_f32([0.1, 0.1, 0.2, nan]),
    )
    assert abs(values[0] - 0.9) < 1e-6  # direct replaces lower; global is still additive
    assert abs(values[1] - 0.5) < 1e-6
    assert abs(values[2] - 0.5) < 1e-6  # global-only is a real edit
    assert values[3] == 0.0  # shared effective value for a wholly absent sample
    erased_direct = C.effective_curve(
        C.encode_f32([0.2]), C.encode_f32([0.1]), "energy", 1,
        C.encode_f32([0.1]), C.encode_f32([nan]), C.encode_f32([0.1]),
    )
    assert abs(erased_direct[0] - 0.5) < 1e-6


def test_phrase_absolute_curve_origin_tempo_and_dynamic_effect():
    notes = [{"tick": 0, "duration": 480, "key": 60, "velocity": 1.0,
              "lyric": "a", "phonemes": ["a"], "legato": False}]
    tempos = [{"tick": 0, "bpm": 120.0}, {"tick": 960, "bpm": 60.0}]
    baseline = synth_phrase({"notes": notes, "offset_ticks": 480, "tempos": tempos}, "dsp")
    slow = synth_phrase({"notes": notes, "offset_ticks": 960, "tempos": tempos}, "dsp")
    assert len(slow) > len(baseline) * 1.9
    pitch_values = [float("nan")] * 1440
    pitch_values[480:960] = [300.0] * 480
    pitch_spec = {"notes": notes, "offset_ticks": 480, "tempos": tempos,
                  "curves": {"pitch": {"direct": C.encode_f32(pitch_values)}}}
    pitched = synth_phrase(pitch_spec, "dsp")
    assert not np.allclose(pitched, baseline)
    dynamic_spec = {"notes": notes, "offset_ticks": 480, "tempos": tempos,
                    "curves": {"dynamic": {"direct": C.encode_f32([float("nan")] * 480 + [0.0] * 960)}}}
    quiet = synth_phrase(dynamic_spec, "dsp")
    assert float(np.max(np.abs(quiet))) < float(np.max(np.abs(baseline))) * 0.02
    notes = [
        {"tick": 0, "duration": 480, "key": 60, "velocity": 1.0, "lyric": "a", "phonemes": [], "legato": False},
        {"tick": 480, "duration": 480, "key": 62, "velocity": 1.0, "lyric": "b", "phonemes": [], "legato": False},
        {"tick": 1920, "duration": 480, "key": 64, "velocity": 1.0, "lyric": "c", "phonemes": [], "legato": False},
    ]
    groups = C.split_phrases(notes)
    assert [len(g) for g in groups] == [2, 1]


def test_native_http_rejects_unsupported_model_controls_before_inference():
    from inference_service.service_runner import app
    spec = {"hash": "unsupported-layer-regression", "notes": [{"tick": 0, "duration": 240, "key": 60}],
            "tempos": [{"tick": 0, "bpm": 120.0}],
            "curves": {"falsetto": {"direct": C.encode_f32([1.0])}}}
    with app.test_client() as client:
        response = client.post("/render_ds", json=spec)
        assert response.status_code == 422
        assert response.json["error"] == "UNSUPPORTED_CONTROLS"
        assert "falsetto" in response.json["detail"]


def test_native_segments_preserve_score_time_including_leading_rests():
    from inference_service.render_ds import _segment_events
    events = [
        {"dur_q": 8.0, "is_rest": True},
        {"dur_q": 8.0, "is_rest": False},
        {"dur_q": 4.0, "is_rest": True},
        {"dur_q": 8.0, "is_rest": False},
        {"dur_q": 8.0, "is_rest": False},
    ]
    groups = _segment_events(events, 0.5, max_seconds=9.0)
    assert [offset for _, offset in groups] == [0.0, 8.0, 14.0]
    observed = []
    for segment, offset in groups:
        elapsed = offset
        for event in segment:
            if not event["is_rest"]:
                observed.append(elapsed)
            elapsed += event["dur_q"] * 0.5
    assert observed == [4.0, 10.0, 14.0]


def test_native_sparse_layers_preserve_prediction_and_direct_precedence():
    from inference_service.native_pipeline import decode_layers, sample_layers, frame_ticks
    layers = decode_layers({'air': {
        'user': C.encode_f32([.1, float('nan'), .1]),
        'direct': C.encode_f32([float('nan'), .8, float('nan')]),
        'global': C.encode_f32([0., -.2, float('nan')]),
    }})['air']
    result = sample_layers(layers, np.array([-1, 0, 1, 2, 3]), np.array([.4]*5), 'air')
    np.testing.assert_allclose(result, [.4, .5, .6, .5, .4], atol=1e-6)
    ticks = frame_ticks(4, .25, 1., .25, [{'tick': 0, 'bpm': 120.}])
    np.testing.assert_array_equal(ticks, [720, 960, 1200, 1440])


def test_native_long_rest_advances_offset_without_synthesizing_silence():
    from inference_service.render_ds import _segment_events
    groups = _segment_events([
        {'dur_q': 1., 'is_rest': False},
        {'dur_q': 240., 'is_rest': True},
        {'dur_q': 1., 'is_rest': False},
    ], .5)
    assert [offset for _, offset in groups] == [0., 120.5]
    assert all(sum(e['dur_q'] * .5 for e in events) <= 18. for events, _ in groups)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("DSP 测试全部通过 ✓")
