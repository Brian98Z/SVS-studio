"""Spawned-process implementation for native VST3 operations."""
from __future__ import annotations

import multiprocessing as mp
import traceback


def _worker_entry(conn, operation, payload):
    try:
        from . import vst_host
        if operation == "inspect":
            plugin = vst_host.open_effect(payload["path"])
            result = vst_host.enumerate_parameters(plugin)
        elif operation == "process":
            import numpy as np
            plugin = vst_host.open_effect(payload["path"])
            vst_host.restore_plugin_state(plugin, payload.get("state_base64"))
            for name, value in payload["parameters"].items():
                vst_host.set_parameter(plugin, name, value)
            audio = payload["audio"]
            rate = payload["sample_rate"]
            block_frames = payload["block_frames"]
            tail_seconds = payload["tail_seconds"]
            extra = int(round(rate * tail_seconds))
            source = np.asarray(audio, dtype=np.float32)
            padded = np.pad(source, ((0, 0), (0, extra))) if extra else source
            result = vst_host.process_stereo(plugin, padded, rate, block_frames=block_frames)
        else:
            raise ValueError(f"Unknown VST worker operation: {operation}")
        if operation == "process":
            import numpy as np
            result = (result.shape, result.dtype.str, result.tobytes())
        conn.send((True, result))
    except BaseException as exc:
        try:
            conn.send((False, (type(exc).__name__, str(exc), traceback.format_exc())))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        conn.close()


def run_worker(operation, payload, *, timeout=60.0):
    """Run exactly one native operation in a disposable spawned process."""
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker_entry, args=(child, operation, payload))
    process.daemon = False
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            process.terminate()
            process.join(5)
            raise TimeoutError(f"VST {operation} worker exceeded {timeout:g} seconds")
        if process.exitcode not in (0, None) and not parent.poll():
            raise RuntimeError(f"VST {operation} worker crashed (exit code {process.exitcode})")
        try:
            ok, result = parent.recv()
        except EOFError as exc:
            raise RuntimeError(f"VST {operation} worker exited without a response (exit code {process.exitcode})") from exc
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        if not ok:
            kind, message, detail = result
            raise RuntimeError(f"{kind}: {message}\n{detail}")
        if process.exitcode not in (0, None):
            raise RuntimeError(f"VST {operation} worker exited with code {process.exitcode}")
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(5)
