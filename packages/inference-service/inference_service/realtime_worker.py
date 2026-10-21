"""Persistent spawned worker for block-streaming native mixer sessions."""
from __future__ import annotations

import math
import multiprocessing as mp
import queue
import threading
import time

def _worker_main(conn, sample_rate, block_frames, tracks, mixer):
    # Frozen bootloaders can have already fixed the process DPI mode; the
    # editor thread must still use the host's per-monitor-v2 context.
    import sys
    if sys.platform == "win32":
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_dpi = user32.SetProcessDpiAwarenessContext
        set_dpi.argtypes = [ctypes.c_void_p]
        set_dpi.restype = ctypes.c_int
        if not set_dpi(ctypes.c_void_p(-4)) and ctypes.get_last_error() != 5:
            raise OSError(ctypes.get_last_error(), "cannot initialize native worker DPI awareness")
        set_thread_dpi = user32.SetThreadDpiAwarenessContext
        set_thread_dpi.argtypes = [ctypes.c_void_p]
        set_thread_dpi.restype = ctypes.c_void_p
        if not set_thread_dpi(ctypes.c_void_p(-4)):
            raise OSError(ctypes.get_last_error(), "cannot initialize editor thread DPI awareness")
    import numpy as np
    from scipy.signal import lfilter
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
            if not len(x):
                return np.empty_like(x)
            # lfilter's transposed direct-form state corresponding to the
            # same x1/x2/y1/y2 history used by the scalar recurrence.
            zi = (b1*x1+b2*x2-e*y1-f*y2, b2*x1-f*y1)
            y, _ = lfilter((b0,b1,b2), (1.,e,f), x, zi=zi)
            self.filter_state[channel,key] = (float(x[-1]), x1 if len(x) < 2 else float(x[-2]), float(y[-1]), y1 if len(y) < 2 else float(y[-2]))
            return y.astype(x.dtype, copy=False)

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
                    ring = self.reverb[ch]
                    pos = self.pos[ch]
                    offset = 0
                    while offset < len(x):
                        count = min(len(x) - offset, len(ring) - pos)
                        stop = offset + count
                        delayed = ring[pos:pos + count].copy()
                        wet[offset:stop, ch] = delayed
                        # Compute feedback in double precision before the ring's
                        # float32 store, matching the original scalar recurrence.
                        ring[pos:pos + count] = x[offset:stop, ch] + delayed.astype(np.float64)*decay
                        pos = (pos + count) % len(ring)
                        offset = stop
                    self.pos[ch] = pos
                return x*(1-spec["wet"])+wet*spec["wet"]*.35
            if self.kind == "compressor":
                out=x.copy()
                for n in range(len(out)):
                    left, right = float(out[n, 0]), float(out[n, 1])
                    peak = max(abs(left), abs(right))
                    coef=self.attack if peak>self.env else self.release
                    self.env=coef*self.env+(1-coef)*peak
                    if self.env>self.threshold:
                        gain=self.threshold*(self.env/self.threshold)**(1/spec["ratio"])/self.env
                        out[n, 0] *= gain
                        out[n, 1] *= gain
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
            x=acc.get(bid)
            if x is None: x=np.zeros((block_frames,2),np.float32)
            x=x*cfg["volume"]
            x=mixer_module.pan_stereo(x,cfg["pan"])
            x=bus_fx[bid].run(x)
            send(acc, "bus", bid, cfg, x)
        x=acc.get("master")
        if x is None: x=np.zeros((block_frames,2),np.float32)
        x=x*mixer["master"]["volume"]
        return master_fx.run(x)
    editor_commands = queue.Queue()
    editor_lock = threading.RLock()
    editor_result = {"state": "idle", "revision": 0}
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
            allowed = {"state", "kind", "id", "index", "path", "parameters", "state_base64", "error",
                       "worker_pid", "editor_hwnd", "editor_class", "revision"}
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
                                revision = editor_result.get("revision", 0) + 1
                                editor_result.clear()
                                editor_result.update(state="opening", kind=key[0], id=key[1], index=key[2], path=path,
                                                     close_event=close_event, revision=revision, worker_pid=__import__("os").getpid())
                                editor_commands.put((effect, close_event, revision))
                        if already_open:
                            conn.send((True, {"command_error": "EDITOR_ALREADY_OPEN"}))
                            continue
                        conn.send((True, editor_snapshot()))
                    except BaseException:
                        conn.send((True, {"command_error": "EDITOR_TARGET_NOT_FOUND"}))
                elif op == "editor_status":
                    conn.send((True, editor_snapshot()))
                elif op == "editor_close":
                    with editor_lock:
                        closing = editor_result.get("state") == "opening"
                        if closing: editor_result["close_event"].set()
                        revision = editor_result.get("revision")
                    if closing:
                        deadline = time.monotonic() + 3.0
                        while time.monotonic() < deadline:
                            with editor_lock:
                                if editor_result.get("revision") != revision or editor_result.get("state") in {"completed", "failed"}:
                                    break
                            time.sleep(.02)
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

    def run_editor(effect, close_event, revision):
        import os
        import sys
        hwnds = []
        stop_discovery = threading.Event()
        finder = None
        if sys.platform == "win32":
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            HWND = ctypes.c_void_p
            DWORD = ctypes.c_ulong
            BOOL = ctypes.c_int
            user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
            user32.GetWindowThreadProcessId.restype = DWORD
            user32.IsWindowVisible.argtypes = [HWND]
            user32.IsWindowVisible.restype = BOOL
            user32.GetClassNameW.argtypes = [HWND, ctypes.POINTER(ctypes.c_wchar), ctypes.c_int]
            user32.GetClassNameW.restype = ctypes.c_int
            enum_proc = ctypes.WINFUNCTYPE(BOOL, HWND, ctypes.c_void_p)
            user32.EnumWindows.argtypes = [enum_proc, ctypes.c_void_p]
            user32.EnumWindows.restype = BOOL
            @enum_proc
            def collect(hwnd, _):
                pid = DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == os.getpid() and user32.IsWindowVisible(hwnd):
                    name = ctypes.create_unicode_buffer(256)
                    length = user32.GetClassNameW(hwnd, name, len(name))
                    if length > 0 and name.value.startswith("JUCE_"):
                        hwnds.append((int(hwnd), name.value))
                return True
            def discover():
                deadline = time.monotonic() + 5
                while not stop_discovery.is_set() and not close_event.is_set() and time.monotonic() < deadline:
                    hwnds.clear()
                    user32.EnumWindows(collect, None)
                    with editor_lock:
                        if hwnds and editor_result.get("revision") == revision:
                            editor_result.update(editor_hwnd=hwnds[0][0], editor_class=hwnds[0][1])
                    if hwnds: return
                    stop_discovery.wait(.1)
                if not stop_discovery.is_set() and not close_event.is_set():
                    with editor_lock:
                        if editor_result.get("revision") == revision and editor_result.get("state") == "opening":
                            editor_result.update(state="failed", error="native plugin editor window was not found within five seconds")
                            close_event.set()
            finder = threading.Thread(target=discover, name="vst-editor-window-discovery", daemon=False)
            finder.start()
        try:
            effect.plugin.show_editor(close_event)
            if finder:
                finder.join(timeout=.2)
            with editor_lock:
                window_info = {key: editor_result.get(key) for key in ("editor_hwnd", "editor_class")}
            if sys.platform == "win32" and not window_info.get("editor_hwnd"):
                result = {"state": "failed", "kind": editor_result["kind"], "id": editor_result["id"],
                          "index": editor_result["index"], "path": editor_result["path"],
                          "error": "native plugin editor window was not found within five seconds", "revision": revision,
                          "worker_pid": os.getpid()}
            else:
                with plugin_lock:
                    parameters = vst_host.enumerate_parameters(effect.plugin)
                    state_base64 = vst_host.capture_plugin_state(effect.plugin)
                result = {"state": "completed", "kind": editor_result["kind"], "id": editor_result["id"],
                          "index": editor_result["index"], "path": editor_result["path"],
                          "parameters": parameters, "state_base64": state_base64, "revision": revision,
                          "worker_pid": os.getpid(), **window_info}
        except BaseException:
            result = {"state": "failed", "kind": editor_result.get("kind"), "id": editor_result.get("id"),
                      "index": editor_result.get("index"), "path": editor_result.get("path"),
                      "error": "native plugin editor failed or could not capture its final state", "revision": revision,
                      "worker_pid": os.getpid()}
        finally:
            stop_discovery.set()
            if finder:
                finder.join()
        with editor_lock:
            if editor_result.get("revision") == revision:
                editor_result.clear()
                editor_result.update(result)

    conn.send((True, {"latency_samples": total_latency}))
    command_thread = threading.Thread(target=command_loop, name="realtime-mixer-commands", daemon=True)
    command_thread.start()
    try:
        while not shutdown.is_set() or editor_result.get("state") == "opening":
            try:
                effect, close_event, revision = editor_commands.get(timeout=.05)
            except queue.Empty:
                continue
            run_editor(effect, close_event, revision)
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
