"""Bounded sample delays and route arrival planning for continuous stereo audio."""
import numpy as np


class SampleDelay:
    def __init__(self, samples):
        self.ring = np.zeros((samples, 2), np.float32)
        self.pos = 0

    def process(self, audio):
        if not len(self.ring):
            return audio
        out = np.empty_like(audio)
        offset = 0
        while offset < len(audio):
            count = min(len(audio) - offset, len(self.ring) - self.pos)
            out[offset:offset + count] = self.ring[self.pos:self.pos + count]
            self.ring[self.pos:self.pos + count] = audio[offset:offset + count]
            offset += count
            self.pos = (self.pos + count) % len(self.ring)
        return out


def route_delays(tracks, buses, track_latency, bus_latency, master_latency, limit):
    """Return bus order, per-edge delays, and fixed end-to-end latency."""
    incoming = {key: [] for key in ['master', *buses]}
    edges = {}
    def connect(kind, key, config, arrival):
        destinations = [config.get('bus', 'master') if kind == 'track' else config['output']]
        destinations += [s['to'] for s in config['sends']]
        for index, dest in enumerate(destinations):
            edge = (kind, key, index)
            incoming[dest].append((edge, arrival))
    for key, config in tracks.items():
        connect('track', key, config, track_latency[key])
    order = []
    pending = set(buses)
    while pending:
        ready = [key for key in buses if key in pending and not any(
            buses[src]['output'] == key or any(s['to'] == key for s in buses[src]['sends'])
            for src in pending)]
        if not ready:
            raise ValueError('bus routing contains a cycle')
        for key in ready:
            inputs = incoming[key]
            arrival = max((n for _, n in inputs), default=0)
            for edge, value in inputs:
                edges[edge] = arrival - value
            # Disconnected buses produce no timeline source and cannot delay live paths.
            if inputs:
                connect('bus', key, buses[key], arrival + bus_latency[key])
            order.append(key)
            pending.remove(key)
    arrival = max((n for _, n in incoming['master']), default=0)
    for edge, value in incoming['master']:
        edges[edge] = arrival - value
    total = arrival + master_latency
    if total > limit:
        raise ValueError('native route latency exceeds 10 seconds')
    return order, edges, total
