"""Persistent spawned worker for block-streaming native mixer sessions."""
from __future__ import annotations

import math
import multiprocessing as mp
import queue
import threading

def _worker_main(conn, sample_rate, block_frames, tracks, mixer):
    import numpy as np
    from . import mixer as mixer_module
    from . import vst_host
    from .stream_delay import SampleDelay, route_delays

    plugin_lock = threading.RLock()
    delay_samples = 0
    def reserve_delay(samples):
        nonlocal delay_samples
        delay_samples += samples
        if delay_samples * 8 > 64 * 1024 * 1024:
            raise ValueError("native compensation delay memory exceeds 64 MiB")

    class Effect:
        def __init__(self, spec):
            self.kind = spec["type"]
            self.plugin = vst_host.open_effect(spec["path"]) if self.kind == "vst3" else None
            self.spec = spec
            self.latency = 0
            self.filter_state = {}
            self.reverb = None
            self.pos = [0, 0]
            self.env = 0.0
            if self.plugin:
                self.state_base64 = spec.get("state_base64")
                vst_host.restore_plugin_state(self.plugin, self.state_base64)
            if self.plugin:
                for name, value in spec["parameters"].items():
                    vst_host.set_parameter(self.plugin, name, value)
                # Initialize the host's sample rate, then reset its time origin.
                self.plugin.process(np.zeros((2, block_frames), np.float32), sample_rate=float(sample_rate), reset=True, buffer_size=block_frames)
                self.plugin.reset()
                self.latency = self.plugin.reported_latency_samples
                if isinstance(self.latency, bool) or not isinstance(self.latency, int) or not 0 <= self.latency <= int(sample_rate * 10):
                    raise ValueError("invalid plugin reported latency")
                # reset() clears Pedalboard's prepared-format cache. Prime again
                # on this thread so first-process preparation remains on it.
                primed = np.asarray(self.plugin.process(np.zeros((2, block_frames), np.float32), sample_rate=float(sample_rate), reset=False, buffer_size=block_frames))
                if self.plugin.reported_latency_samples != self.latency:
                    raise RuntimeError("plugin latency changed; restart playback")
                self.startup_remaining = max(0, self.latency - (block_frames - primed.shape[1]))
            else:
                self.startup_remaining = self.latency
            reserve_delay(self.latency)
            self.bypass_delay = SampleDelay(self.latency)
            self.configure(spec)

        def configure(self, spec):
            self.spec = spec
            if self.kind == "reverb" and self.reverb is None:
                self.reverb = [np.zeros(max(1, int(sample_rate * (.035 + .11 * spec["room"]) * (1 + .08 * ch))), np.float32) for ch in range(2)]
            if self.kind == "compressor":
                self.threshold = 10 ** (spec["threshold_db"] / 20)
                self.attack = math.exp(-1 / (sample_rate * spec["attack_ms"] * .001))
                self.release = math.exp(-1 / (sample_rate * spec["release_ms"] * .001))

        def update(self, spec):
            if self.plugin:
                old = self.spec["parameters"]
                if spec.get("state_base64") != self.state_base64:
                    raise RuntimeError("plugin state changed; restart playback")
                # Preflight all names before mutating any plugin parameter.
                missing = [name for name in spec["parameters"] if self.plugin.parameters.get(name) is None]
                if missing:
                    raise ValueError("unknown VST3 parameter")
                for name, value in spec["parameters"].items():
                    if old.get(name) != value:
                        vst_host.set_parameter(self.plugin, name, value)
                if self.plugin.reported_latency_samples != self.latency:
                    raise RuntimeError("plugin latency changed; restart playback")
            self.configure(spec)

        def filt(self, x, channel, key, hz, highpass=False):
            w = 2 * math.pi * min(hz, sample_rate * .45) / sample_rate
            c, s = math.cos(w), math.sin(w)
            a, d = s / math.sqrt(2), 1 + s / math.sqrt(2)
            if highpass:
                b0, b1, b2 = (1+c)/2/d, -(1+c)/d, (1+c)/2/d
            else:
                b0, b1, b2 = (1-c)/2/d, (1-c)/d, (1-c)/2/d
            e, f = -2*c/d, (1-a)/d
            x1, x2, y1, y2 = self.filter_state.get((channel,key),(0.,0.,0.,0.))
            y = np.empty_like(x)
            for n, sample in enumerate(x):
                sample = float(sample)
                value = b0*sample+b1*x1+b2*x2-e*y1-f*y2
                y[n] = value
                x2,x1,y2,y1 = x1,sample,y1,value
            self.filter_state[channel,key] = (x1,x2,y1,y2)
            return y

        def run(self, x):
            spec = self.spec
            if self.plugin:
                dry = self.bypass_delay.process(x)
                with plugin_lock:
                    rendered = np.asarray(self.plugin.process(np.ascontiguousarray(x.T), sample_rate=float(sample_rate), reset=False, buffer_size=block_frames), dtype=np.float32)
                    if self.plugin.reported_latency_samples != self.latency:
                        raise RuntimeError("plugin latency changed; restart playback")
                    if rendered.ndim != 2 or rendered.shape[0] != 2 or rendered.shape[1] > len(x) or not np.isfinite(rendered).all():
                        raise RuntimeError("VST3 returned invalid audio")
                    withheld = len(x) - rendered.shape[1]
                    if withheld > self.startup_remaining:
                        raise RuntimeError("plugin buffering exceeds reported latency; restart playback")
                    self.startup_remaining -= withheld
                    if withheld:
                        wet = np.zeros_like(x)
                        wet[withheld:] = rendered.T
                    else:
                        wet = rendered.T
                return wet if spec["enabled"] else dry
            if not spec["enabled"]: return x
            if self.kind == "eq3":
                out = x.copy()
                for ch in range(2):
                    lo = self.filt(x[:,ch],ch,"low",spec["low_hz"])
                    below = self.filt(x[:,ch],ch,"high",spec["high_hz"])
                    high = x[:,ch]-below
                    mid = below-lo
                    out[:,ch] = lo*10**(spec["low_db"]/20)+mid*10**(spec["mid_db"]/20)+high*10**(spec["high_db"]/20)
                return out
            if self.kind == "reverb":
                wet = np.empty_like(x)
                decay = .25 + .65*spec["room"]
                for ch in range(2):
                    ring = self.reverb[ch]; pos = self.pos[ch]
                    for n in range(len(x)):
                        delayed = ring[pos]
                        ring[pos] = x[n,ch]+delayed*decay
                        wet[n,ch] = delayed
                        pos = (pos+1)%len(ring)
                    self.pos[ch] = pos
                return x*(1-spec["wet"])+wet*spec["wet"]*.35
            if self.kind == "compressor":
                out=x.copy()
                for n in range(len(out)):
                    peak=float(np.max(np.abs(out[n])))
                    coef=self.attack if peak>self.env else self.release
                    self.env=coef*self.env+(1-coef)*peak
                    if self.env>self.threshold: out[n]*=self.threshold*(self.env/self.threshold)**(1/spec["ratio"])/self.env
                return out

    class Chain:
        def __init__(self,specs): self.effects=[Effect(spec) for spec in specs]
        def run(self,x):
            for effect in self.effects: x=effect.run(x)
            return x
        def update(self,specs):
            if len(specs)!=len(self.effects): raise ValueError("effect topology changed")
            # Preflight every VST parameter name/range before applying any update.
            for effect,spec in zip(self.effects,specs):
                if effect.plugin:
                    for name,value in spec["parameters"].items():
                        parameter=effect.plugin.parameters.get(name)
                        if parameter is None: raise ValueError("unknown VST3 parameter")
                        if isinstance(value,(int,float)) and not isinstance(value,bool):
                            if not math.isfinite(float(value)): raise ValueError("invalid VST3 parameter value")
                            low,high=getattr(parameter,"min_value",None),getattr(parameter,"max_value",None)
                            if low is not None and value<low or high is not None and value>high: raise ValueError("VST3 parameter value out of range")
            for effect,spec in zip(self.effects,specs): effect.update(spec)

    track_cfg={t["id"]:t for t in tracks}
    track_fx={t["id"]:Chain(t["fx"]) for t in tracks}
    bus_cfg={b["id"]:b for b in mixer["buses"]}
    bus_fx={b["id"]:Chain(b["effects"]) for b in mixer["buses"]}
    master_fx=Chain(mixer["master"]["effects"])
    bus_order, edge_samples, total_latency = route_delays(
        track_cfg, bus_cfg,
        {key: sum(e.latency for e in chain.effects) for key, chain in track_fx.items()},
        {key: sum(e.latency for e in chain.effects) for key, chain in bus_fx.items()},
        sum(e.latency for e in master_fx.effects), int(sample_rate * 10))
    reserve_delay(sum(edge_samples.values()))
    edge_delays = {key: SampleDelay(value) for key, value in edge_samples.items()}

    def send(acc, kind, key, config, audio):
        destinations = [(config.get("bus", "master") if kind == "track" else config["output"], 1)]
        destinations += [(s["to"], s["level"]) for s in config["sends"]]
        for index, (dest, level) in enumerate(destinations):
            delay = edge_delays.get((kind, key, index))
            if delay is not None:
                add(acc, dest, delay.process(audio * level))

    def add(acc,key,x):
        if key in acc: acc[key] += x
        else: acc[key] = x.copy()

    def route(inputs):
        acc={}; solo=any(t["solo"] for t in track_cfg.values())
        for tid,audio in inputs.items():
            cfg=track_cfg[tid]
            x=track_fx[tid].run(audio*(cfg["volume"]*cfg["bus_gain"]))
            if cfg["mute"] or (solo and not cfg["solo"]): x=np.zeros_like(x)
            send(acc, "track", tid, cfg, x)
        for bid in bus_order:
            cfg=bus_cfg[bid]
            x=acc.get(bid,np.zeros((block_frames,2),np.float32))*cfg["volume"]
            x=mixer_module.pan_stereo(x,cfg["pan"])
            x=bus_fx[bid].run(x)
            send(acc, "bus", bid, cfg, x)
        x=acc.get("master",np.zeros((block_frames,2),np.float32))*mixer["master"]["volume"]
        return master_fx.run(x)
    editor_commands = queue.Queue()
    editor_lock = threading.RLock()
    editor_result = {"state": "idle"}
    shutdown = threading.Event()


    editor_targets = {}
    for track_id, chain in track_fx.items():
        for index, effect in enumerate(chain.effects):
            editor_targets[("track", track_id, index)] = effect
    for bus_id, chain in bus_fx.items():
        for index, effect in enumerate(chain.effects):
            editor_targets[("bus", bus_id, index)] = effect
    for index, effect in enumerate(master_fx.effects):
        editor_targets[("master", "master", index)] = effect

    def editor_snapshot():
        with editor_lock:
            allowed = {"state", "kind", "id", "index", "path", "parameters", "state_base64", "error"}
            return {key: value for key, value in editor_result.items() if key in allowed}

    def command_loop():
        try:
            while True:
                msg = conn.recv()
                op = msg[0]
                if op == "close":
                    shutdown.set()
                    with editor_lock:
                        if editor_result.get("state") == "opening": editor_result["close_event"].set()
                    conn.send((True, None))
                    break
                if op == "process":
                    with plugin_lock:
                        result = route(msg[1])
                    if not np.isfinite(result).all(): raise RuntimeError("mixer produced non-finite audio")
                    conn.send((True, np.ascontiguousarray(result, dtype=np.float32).tobytes()))
                elif op == "update":
                    new_tracks, new_mixer = msg[1], msg[2]
                    with plugin_lock:
                        for tid, cfg in new_tracks.items(): track_fx[tid].update(cfg["fx"])
                        for bid, cfg in bus_cfg.items(): bus_fx[bid].update(new_mixer["buses_by_id"][bid]["effects"])
                        master_fx.update(new_mixer["master"]["effects"])
                        track_cfg.clear(); track_cfg.update(new_tracks)
                        bus_cfg.clear(); bus_cfg.update(new_mixer["buses_by_id"])
                        mixer.update(new_mixer)
                    conn.send((True, None))
                elif op == "editor_open":
                    try:
                        target = msg[1]
                        kind, target_id, index = target.get("kind"), target.get("id"), target.get("index")
                        if type(kind) is not str or type(index) is not int or type(target_id) is not str:
                            command_error = "EDITOR_TARGET_NOT_FOUND"
                        else:
                            key = (kind, "master" if kind == "master" else target_id, index)
                            effect = editor_targets.get(key)
                            if effect is None:
                                command_error = "EDITOR_TARGET_NOT_FOUND"
                            elif effect.plugin is None:
                                command_error = "EDITOR_TARGET_NOT_VST3"
                            else:
                                command_error = None
                        if command_error:
                            conn.send((True, {"command_error": command_error}))
                            continue
                        with editor_lock:
                            already_open = editor_result.get("state") == "opening"
                            if not already_open:
                                path = effect.spec["path"]
                                close_event = threading.Event()
                                editor_result.clear()
                                editor_result.update(state="opening", kind=key[0], id=key[1], index=key[2], path=path, close_event=close_event)
                                editor_commands.put((effect, close_event))
                        if already_open:
                            conn.send((True, {"command_error": "EDITOR_ALREADY_OPEN"}))
                            continue
                        conn.send((True, {"state": "opening", "kind": key[0], "id": key[1], "index": key[2], "path": path}))
                    except BaseException:
                        conn.send((True, {"command_error": "EDITOR_TARGET_NOT_FOUND"}))
                elif op == "editor_status":
                    conn.send((True, editor_snapshot()))
                elif op == "editor_close":
                    with editor_lock:
                        if editor_result.get("state") == "opening": editor_result["close_event"].set()
                    conn.send((True, editor_snapshot()))
                else:
                    raise ValueError("unknown worker command")
        except (EOFError, BrokenPipeError, OSError):
            shutdown.set()
            with editor_lock:
                if editor_result.get("state") == "opening": editor_result["close_event"].set()
        except BaseException as exc:
            shutdown.set()
            try: conn.send((False, (type(exc).__name__, str(exc))))
            except (BrokenPipeError, EOFError, OSError): pass
        finally:
            shutdown.set()

    def run_editor(effect, close_event):
        try:
            # The native editor must run on this worker's main thread. Processing
            # continues on command_loop's thread and never shares the pipe.
            effect.plugin.show_editor(close_event)
            with plugin_lock:
                parameters = vst_host.enumerate_parameters(effect.plugin)
                state_base64 = vst_host.capture_plugin_state(effect.plugin)
            result = {"state": "completed", "kind": editor_result["kind"], "id": editor_result["id"],
                      "index": editor_result["index"], "path": editor_result["path"],
                      "parameters": parameters, "state_base64": state_base64}
        except BaseException:
            result = {"state": "failed", "kind": editor_result.get("kind"), "id": editor_result.get("id"),
                      "index": editor_result.get("index"), "path": editor_result.get("path"),
                      "error": "native plugin editor failed or could not capture its final state"}
        with editor_lock:
            editor_result.clear()
            editor_result.update(result)

    conn.send((True, {"latency_samples": total_latency}))
    command_thread = threading.Thread(target=command_loop, name="realtime-mixer-commands", daemon=True)
    command_thread.start()
    try:
        while not shutdown.is_set() or editor_result.get("state") == "opening":
            try:
                effect, close_event = editor_commands.get(timeout=.05)
            except queue.Empty:
                continue
            run_editor(effect, close_event)
    finally:
        shutdown.set()
        command_thread.join(1)
        conn.close()


def start_worker(sample_rate,block_frames,tracks,mixer):
    context=mp.get_context("spawn")
    parent,child=context.Pipe(duplex=True)
    process=context.Process(target=_worker_main,args=(child,sample_rate,block_frames,tracks,mixer),daemon=True)
    process.start(); child.close()
    if not parent.poll(5.0):
        if process.is_alive(): process.terminate()
        process.join(1); parent.close()
        raise TimeoutError("native mixer worker initialization timed out")
    try: ok,result=parent.recv()
    except (EOFError,OSError) as exc:
        process.join(1); parent.close()
        raise RuntimeError("native mixer worker initialization failed") from exc
    if not ok or not isinstance(result, dict) or "latency_samples" not in result:
        process.join(1); parent.close()
        raise RuntimeError("native mixer worker initialization failed")
    return process,parent,result["latency_samples"]
