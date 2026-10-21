import threading
from queue import Queue

import numpy as np
import pytest

from inference_service.stream_delay import SampleDelay, route_delays
from inference_service import realtime_worker, vst_host


@pytest.mark.parametrize('delay', [0, 2, 19])
def test_sample_delay_preserves_signal_across_arbitrary_blocks(delay):
    line = SampleDelay(delay)
    signal = np.arange(80, dtype=np.float32).reshape(40, 2)
    padded = np.pad(signal, ((0, delay), (0, 0)))
    chunks = [line.process(padded[i:i+3]) for i in range(0, len(padded), 3)]
    np.testing.assert_array_equal(np.concatenate(chunks), np.pad(signal, ((delay, 0), (0, 0))))


def test_route_plan_aligns_nested_sends_and_ignores_disconnected_bus():
    tracks = {'dry': {'bus': 'master', 'sends': []}, 'wet': {'bus': 'a', 'sends': [{'to': 'master'}]}}
    buses = {'a': {'output': 'b', 'sends': [{'to': 'master'}]}, 'b': {'output': 'master', 'sends': []}, 'unused': {'output': 'master', 'sends': []}}
    order, delays, total = route_delays(tracks, buses, {'dry': 0, 'wet': 3}, {'a': 5, 'b': 7, 'unused': 999}, 11, 100)
    assert total == 26
    assert delays[('track', 'dry', 0)] == 15
    assert delays[('track', 'wet', 1)] == 12
    assert delays[('bus', 'a', 1)] == 7
    assert ('bus', 'unused', 0) not in delays
    assert order.index('a') < order.index('b')


@pytest.mark.parametrize('withheld', [False, True])
def test_worker_stream_alignment_and_bypass_keep_fixed_budget(monkeypatch, withheld):
    class Plugin:
        reported_latency_samples = 13
        parameters = {}
        def __init__(self): self.reset()
        def reset(self):
            self.delay = SampleDelay(13)
            self.omit = 13 if withheld else 0
        def process(self, audio, **kwargs):
            if kwargs.get('reset'): self.reset()
            out = self.delay.process(audio.T).T
            count = min(self.omit, out.shape[1])
            self.omit -= count
            return out[:, count:]
    monkeypatch.setattr(vst_host, 'open_effect', lambda path: Plugin())
    fx = {'type': 'vst3', 'path': 'fixture', 'parameters': {}, 'enabled': True}
    tracks = [{'id': name, 'volume': 1, 'bus_gain': 1, 'mute': False, 'solo': False,
               'bus': 'master', 'sends': [], 'fx': [dict(fx)] if name == 'wet' else []} for name in ['dry', 'wet']]
    mixer = {'buses': [], 'master': {'volume': 1, 'effects': []}, 'buses_by_id': {}}
    commands, replies = Queue(), Queue()
    class Connection:
        def recv(self): return commands.get(timeout=5)
        def send(self, value): replies.put(value)
        def close(self): pass
    worker = threading.Thread(target=realtime_worker._worker_main, args=(Connection(), 48000, 8, tracks, mixer), daemon=True)
    worker.start()
    assert replies.get(timeout=5) == (True, {'latency_samples': 13})
    signal = np.zeros((80, 2), np.float32)
    signal[2] = .25
    signal[31] = -.125
    signal[60] = .375
    output = []
    for seq in range(10):
        if seq == 4:
            updated = {t['id']: {**t, 'fx': [{**fx, 'enabled': False}] if t['id'] == 'wet' else []} for t in tracks}
            commands.put(('update', updated, mixer))
            assert replies.get(timeout=5) == (True, None)
        x = signal[seq*8:(seq+1)*8]
        commands.put(('process', {'dry': x, 'wet': x}))
        ok, pcm = replies.get(timeout=5)
        assert ok, pcm
        output.append(np.frombuffer(pcm, np.float32).reshape(-1, 2))
    commands.put(('close',))
    assert replies.get(timeout=5) == (True, None)
    worker.join(timeout=5)
    expected = np.zeros_like(signal)
    expected[13:] = signal[:-13]*2
    np.testing.assert_array_equal(np.concatenate(output), expected)


def test_route_latency_limit_rejects_excessive_serial_delay():
    with pytest.raises(ValueError, match='10 seconds'):
        route_delays({'t': {'bus': 'b', 'sends': []}}, {'b': {'output': 'master', 'sends': []}},
                     {'t': 6}, {'b': 5}, 0, 10)


def test_worker_rejects_parameter_that_changes_latency(monkeypatch):
    class Plugin:
        reported_latency_samples = 2
        parameters = {'delay': object()}
        def process(self, audio, **kwargs): return audio
        def reset(self): pass
    monkeypatch.setattr(vst_host, 'open_effect', lambda path: Plugin())
    monkeypatch.setattr(vst_host, 'set_parameter', lambda plugin, name, value: setattr(plugin, 'reported_latency_samples', value))
    spec = {'type': 'vst3', 'path': 'fixture', 'parameters': {'delay': 2}, 'enabled': True}
    track = {'id': 't', 'volume': 1, 'bus_gain': 1, 'mute': False, 'solo': False, 'bus': 'master', 'sends': [], 'fx': [spec]}
    mixer = {'buses': [], 'master': {'volume': 1, 'effects': []}, 'buses_by_id': {}}
    commands, replies = Queue(), Queue()
    class Connection:
        def recv(self): return commands.get(timeout=5)
        def send(self, value): replies.put(value)
        def close(self): pass
    worker = threading.Thread(target=realtime_worker._worker_main, args=(Connection(), 48000, 8, [track], mixer), daemon=True)
    worker.start()
    assert replies.get(timeout=5) == (True, {'latency_samples': 2})
    commands.put(('update', {'t': {**track, 'fx': [{**spec, 'parameters': {'delay': 3}}]}}, mixer))
    assert replies.get(timeout=5) == (False, ('RuntimeError', 'plugin latency changed; restart playback'))
    worker.join(timeout=5)


def _run_worker_effect(effect, signal, updates=()):
    commands, replies = Queue(), Queue()
    spec = {"enabled": True, **effect}
    track = {"id": "t", "volume": 1, "bus_gain": 1, "mute": False, "solo": False,
             "bus": "master", "sends": [], "fx": [spec]}
    mixer = {"buses": [], "master": {"volume": 1, "effects": []}, "buses_by_id": {}}

    class Connection:
        def recv(self): return commands.get(timeout=5)
        def send(self, value): replies.put(value)
        def close(self): pass

    worker = threading.Thread(target=realtime_worker._worker_main,
                              args=(Connection(), 48000, 16, [track], mixer), daemon=True)
    worker.start()
    assert replies.get(timeout=5)[0]
    blocks = []
    for index, block in enumerate(np.array_split(signal, len(signal) // 16)):
        if index in updates:
            spec = {"enabled": True, **updates[index]}
            commands.put(("update", {"t": {**track, "fx": [spec]}}, mixer))
            assert replies.get(timeout=5) == (True, None)
        commands.put(("process", {"t": block}))
        ok, pcm = replies.get(timeout=5)
        assert ok, pcm
        blocks.append(np.frombuffer(pcm, np.float32).reshape(-1, 2))
    commands.put(("close",))
    assert replies.get(timeout=5) == (True, None)
    worker.join(timeout=5)
    assert not worker.is_alive()
    return np.concatenate(blocks)


def _reference_biquad(signal, rate, hz, state):
    w = 2 * np.pi * min(hz, rate * .45) / rate
    c, s = np.cos(w), np.sin(w)
    a, d = s / np.sqrt(2), 1 + s / np.sqrt(2)
    b = ((1-c)/2/d, (1-c)/d, (1-c)/2/d)
    e, f = -2*c/d, (1-a)/d
    x1, x2, y1, y2 = state
    result = np.empty(len(signal), np.float64)
    for i, sample in enumerate(signal):
        value = b[0]*float(sample)+b[1]*x1+b[2]*x2-e*y1-f*y2
        result[i] = value
        x2, x1, y2, y1 = x1, float(sample), y1, value
    return result, (x1, x2, y1, y2)


def test_realtime_eq_preserves_filter_history_across_blocks_and_cutoff_update():
    rng = np.random.default_rng(24)
    signal = rng.normal(0, .2, (64, 2)).astype(np.float32)
    effect = {"type": "eq3", "low_db": 5, "mid_db": -2, "high_db": 3,
              "low_hz": 180, "high_hz": 5000}
    updated = {**effect, "low_hz": 730}
    out = _run_worker_effect(effect, signal, {2: updated})
    expected = np.empty_like(signal)
    for ch in range(2):
        low_state = high_state = (0., 0., 0., 0.)
        for start, end, cutoff in ((0, 32, 180), (32, 64, 730)):
            low, low_state = _reference_biquad(signal[start:end, ch], 48000, cutoff, low_state)
            below, high_state = _reference_biquad(signal[start:end, ch], 48000, 5000, high_state)
            expected[start:end, ch] = (low*10**(5/20) + (below-low)*10**(-2/20) +
                                       (signal[start:end, ch]-below)*10**(3/20))
    np.testing.assert_allclose(out, expected, rtol=2e-6, atol=2e-7)


def test_realtime_reverb_keeps_ring_history_while_room_and_wet_update():
    effect = {"type": "reverb", "wet": .4, "room": .3}
    updated = {**effect, "wet": .8, "room": .9}
    ring_lengths = [int(48000*(.035+.11*effect["room"])*(1+.08*ch)) for ch in range(2)]
    length = ((max(ring_lengths)*2 + 64 + 15) // 16) * 16
    signal = np.zeros((length, 2), np.float32)
    signal[0] = (.7, -.3)
    signal[max(ring_lengths)//2] = (-.2, .5)
    update_frame = ((max(ring_lengths) + 8) // 16) * 16
    out = _run_worker_effect(effect, signal, {update_frame // 16: updated})
    ring = [np.zeros(size, np.float32) for size in ring_lengths]
    pos = [0, 0]
    expected = np.empty_like(signal)
    for n in range(len(signal)):
        spec = effect if n < update_frame else updated
        decay = .25+.65*spec["room"]
        wet = []
        for ch in range(2):
            delayed = ring[ch][pos[ch]]
            ring[ch][pos[ch]] = signal[n,ch]+delayed*decay
            pos[ch] = (pos[ch]+1)%len(ring[ch])
            wet.append(delayed)
        expected[n] = signal[n]*(1-spec["wet"])+np.asarray(wet)*spec["wet"]*.35
    assert np.any(np.abs(out[update_frame:, :]) > 0)
    np.testing.assert_allclose(out, expected, rtol=2e-7, atol=1e-8)


def test_realtime_compressor_keeps_linked_stereo_envelope_across_blocks():
    signal = np.zeros((64, 2), np.float32)
    signal[0:24, 0] = .8
    signal[24:40, 1] = .4
    effect = {"type": "compressor", "threshold_db": -18, "ratio": 4,
              "attack_ms": 1, "release_ms": 30}
    out = _run_worker_effect(effect, signal)
    threshold = 10**(-18/20)
    attack = np.exp(-1/(48000*.001))
    release = np.exp(-1/(48000*.03))
    env = 0.
    expected = signal.copy()
    for n in range(len(signal)):
        peak = max(abs(float(signal[n,0])), abs(float(signal[n,1])))
        coef = attack if peak > env else release
        env = coef*env+(1-coef)*peak
        if env > threshold:
            expected[n] *= threshold*(env/threshold)**.25/env
    np.testing.assert_allclose(out, expected, rtol=1e-6, atol=1e-7)
