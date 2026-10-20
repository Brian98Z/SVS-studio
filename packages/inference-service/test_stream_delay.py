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
    assert not worker.is_alive()
