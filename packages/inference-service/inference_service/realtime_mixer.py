"""HTTP-owned persistent native mixer sessions."""
from __future__ import annotations

import math
import threading
import time
import uuid

import numpy as np
from flask import jsonify, request

from . import mixer as mixer_module
from .realtime_worker import start_worker
from . import vst_host

MAX_SESSIONS = 4
SESSION_TTL = 30.0
CALL_TIMEOUT = 5.0
MAX_TRACKS = 32
MAX_EFFECTS = 32
MAX_BLOCK_FRAMES = 8192
_sessions = {}
_sessions_lock = threading.RLock()


class SessionError(Exception):
    def __init__(self, code, detail, status):
        super().__init__(detail)
        self.code, self.status = code, status


def _fail(code, detail, status=400):
    raise SessionError(code, detail, status)


def _tracks(raw, sample_rate):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TRACKS:
        _fail("INVALID_TRACKS", f"tracks must contain 1 to {MAX_TRACKS} tracks")
    result, ids, effects = [], set(), 0
    for item in raw:
        if not isinstance(item, dict): _fail("INVALID_TRACK", "each track must be an object")
        tid = item.get("id")
        if not isinstance(tid, str) or not tid or tid in ids: _fail("INVALID_TRACK", "track ids must be unique non-empty strings")
        ids.add(tid)
        try:
            fx = mixer_module._effects(item.get("fx", item.get("effects", [])))
            volume = mixer_module._finite(item.get("volume", 1), "track volume", 0, 4)
            gain = mixer_module._finite(item.get("bus_gain", 1), "bus gain", 0, 4)
            pan = mixer_module._finite(item.get("pan", 0), "pan", -1, 1)
            sends = item.get("sends", [])
            if not isinstance(sends, list): raise ValueError("track sends must be an array")
            clean_sends = []
            for send in sends:
                if not isinstance(send, dict) or not isinstance(send.get("to"), str): raise ValueError("invalid track send")
                clean_sends.append({"to":send["to"],"level":mixer_module._finite(send.get("level",1),"send level",0,4)})
            bus = item.get("bus") or "master"
            if not isinstance(bus, str): raise ValueError("invalid track bus")
            effects += len(fx)
            result.append({"id":tid,"volume":volume,"bus_gain":gain,"pan":pan,"mute":bool(item.get("mute",False)),"solo":bool(item.get("solo",False)),"fx":fx,"sends":clean_sends,"bus":bus})
        except ValueError as exc: _fail("INVALID_CONFIG", str(exc))
    return result, effects


def _config(mixer):
    try:
        cfg = mixer_module.validate_config(mixer if mixer is not None else mixer_module.default_mixer_config())
    except ValueError as exc:
        _fail("INVALID_CONFIG", str(exc))
    cfg["buses_by_id"] = {b["id"]:b for b in cfg["buses"]}
    return cfg

def _validate_topology(tracks, cfg):
    buses = set(cfg["buses_by_id"])
    count = sum(len(t["fx"]) for t in tracks) + sum(len(b["effects"]) for b in cfg["buses"]) + len(cfg["master"]["effects"])
    if count > MAX_EFFECTS: _fail("INVALID_CONFIG", f"at most {MAX_EFFECTS} effects are allowed")
    for t in tracks:
        if t["bus"] != "master" and t["bus"] not in buses: _fail("INVALID_CONFIG", f"unknown track bus: {t['bus']}")
        if any(s["to"] not in buses and s["to"] != "master" for s in t["sends"]): _fail("INVALID_CONFIG", "unknown track send bus")
    return _topology(tracks, cfg)


def _topology(tracks, cfg):
    def chain(effects): return tuple((e["type"], e.get("path"), e.get("state_base64")) for e in effects)
    return (tuple((t["id"], t["bus"], tuple(s["to"] for s in t["sends"]), chain(t["fx"])) for t in tracks),
            tuple((b["id"], b["output"], tuple(s["to"] for s in b["sends"]), chain(b["effects"])) for b in cfg["buses"]), chain(cfg["master"]["effects"]))
def _worker_call(session, message):
    proc, conn = session["proc"], session["conn"]
    if not proc.is_alive(): _fail("SESSION_FAILED", "native mixer worker exited", 422)
    try:
        conn.send(message)
        if not conn.poll(CALL_TIMEOUT):
            _terminate(session)
            _fail("WORKER_TIMEOUT", "native mixer worker exceeded processing timeout", 504)
        ok, result = conn.recv()
    except (EOFError, BrokenPipeError, OSError):
        _terminate(session)
        _fail("SESSION_FAILED", "native mixer worker failed", 422)
    if not ok:
        _terminate(session)
        if isinstance(result, tuple) and len(result) > 1 and result[1] in ("plugin latency changed; restart playback", "plugin state changed; restart playback"):
            _fail("RESTART_REQUIRED", "plugin state or latency changed; restart playback", 409)
        _fail("PLUGIN_PROCESSING_ERROR", "native mixer processing failed", 422)
    session["last"] = time.monotonic()
    return result


def _terminate(session):
    proc, conn = session.get("proc"), session.get("conn")
    try:
        if conn: conn.close()
    except OSError: pass
    if proc and proc.is_alive():
        proc.terminate(); proc.join(1)
    session["closed"] = True


def _cleanup_expired():
    now = time.monotonic()
    for sid, session in list(_sessions.items()):
        if now - session["last"] > SESSION_TTL and session["lock"].acquire(blocking=False):
            try:
                _terminate(session)
                _sessions.pop(sid, None)
            finally:
                session["lock"].release()


def _reaper():
    while True:
        time.sleep(1.0)
        with _sessions_lock:
            _cleanup_expired()


_reaper_started = False


def register_routes(app):
    global _reaper_started
    with _sessions_lock:
        if not _reaper_started:
            threading.Thread(target=_reaper, name="vst-realtime-reaper", daemon=True).start()
            _reaper_started = True
    def endpoint(fn):
        def wrapped():
            try: return fn()
            except SessionError as exc: return jsonify({"error":exc.code,"detail":str(exc)}), exc.status
            except Exception: return jsonify({"error":"REALTIME_ERROR","detail":"realtime mixer request failed"}), 422
        wrapped.__name__ = fn.__name__
        return wrapped

    @app.post("/vst/realtime/open")
    @endpoint
    def open_session():
        body = request.get_json(silent=True)
        if not isinstance(body, dict): _fail("INVALID_REQUEST", "JSON object required")
        rate, frames = body.get("sample_rate"), body.get("block_frames")
        if isinstance(rate, bool) or not isinstance(rate, (int,float)) or not math.isfinite(rate) or not 8000 <= rate <= 192000: _fail("INVALID_CONFIG", "sample_rate must be between 8000 and 192000")
        if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= MAX_BLOCK_FRAMES: _fail("INVALID_CONFIG", f"block_frames must be between 1 and {MAX_BLOCK_FRAMES}")
        tracks, _ = _tracks(body.get("tracks"), rate)
        cfg = _config(body.get("mixer")); topology = _validate_topology(tracks,cfg)
        tracks = [{**t,"pan":0} for t in tracks]
        with _sessions_lock:
            _cleanup_expired()
            if len(_sessions) >= MAX_SESSIONS: _fail("SESSION_CAPACITY", "maximum realtime sessions reached",503)
            sid = uuid.uuid4().hex
            try: proc, conn, latency = start_worker(float(rate),frames,tracks,cfg)
            except Exception: _fail("WORKER_START_FAILED", "could not start native mixer worker",422)
            session={"id":sid,"proc":proc,"conn":conn,"rate":float(rate),"frames":frames,"tracks":tracks,"mixer":cfg,"topology":topology,"sequence":0,"last":time.monotonic(),"lock":threading.Lock(),"closed":False}
            session["latency_samples"] = latency
            _sessions[sid]=session
        return jsonify({"ok":True,"session_id":sid,"block_frames":frames,"sample_rate":rate,"latency_samples":latency})

    @app.post("/vst/realtime/process")
    @endpoint
    def process_session():
        if not request.mimetype or request.mimetype.lower() != "application/octet-stream":
            _fail("INVALID_REQUEST", "Content-Type must be application/octet-stream", 415)
        sid = request.headers.get("X-Audio-Session-Id")
        with _sessions_lock: session = _sessions.get(sid)
        if session is None or session["closed"]: _fail("SESSION_NOT_FOUND", "realtime session is closed or unknown", 404)
        with session["lock"]:
            sequence_header = request.headers.get("X-Audio-Sequence")
            if sequence_header is None or not sequence_header.isascii() or not sequence_header.isdecimal():
                _fail("INVALID_SEQUENCE", f"expected sequence {session['sequence']}")
            sequence = int(sequence_header)
            if sequence != session["sequence"]: _fail("INVALID_SEQUENCE", f"expected sequence {session['sequence']}")
            expected_bytes = session["frames"] * 8 * len(session["tracks"])
            if request.content_length is not None and request.content_length != expected_bytes:
                _fail("INVALID_AUDIO", "audio payload byte length does not match block_frames and track count")
            data = request.stream.read(expected_bytes + 1)
            if len(data) != expected_bytes: _fail("INVALID_AUDIO", "audio payload byte length does not match block_frames and track count")
            interleaved = np.frombuffer(data, dtype="<f4")
            if not np.isfinite(interleaved).all(): _fail("INVALID_AUDIO", "audio samples must be finite")
            track_samples = session["frames"] * 2
            inputs = {track["id"]: interleaved[i * track_samples:(i + 1) * track_samples].reshape((session["frames"], 2))
                      for i, track in enumerate(session["tracks"])}
            started = time.perf_counter()
            output = _worker_call(session, ("process", inputs))
            processing_ms = (time.perf_counter() - started) * 1000
            session["sequence"] += 1
            response = app.response_class(output, mimetype="application/octet-stream")
            response.headers["X-Audio-Sequence"] = str(sequence)
            response.headers["X-Audio-Frames"] = str(session["frames"])
            response.headers["X-Audio-Processing-Ms"] = format(processing_ms, ".6f")
            return response

    @app.post("/vst/realtime/update")
    @endpoint
    def update_session():
        body=request.get_json(silent=True)
        if not isinstance(body,dict): _fail("INVALID_REQUEST","JSON object required")
        sid=body.get("session_id")
        with _sessions_lock: session=_sessions.get(sid)
        if session is None or session["closed"]: _fail("SESSION_NOT_FOUND","realtime session is closed or unknown",404)
        with session["lock"]:
            tracks,_=_tracks(body.get("tracks"),session["rate"]); cfg=_config(body.get("mixer")); topology=_validate_topology(tracks,cfg)
            if topology!=session["topology"]: _fail("RESTART_REQUIRED","effect order, plugin path, preset state, or routing changed; restart playback",409)
            tracks=[{**t,"pan":0} for t in tracks]
            result_cfg={**cfg,"buses_by_id":cfg["buses_by_id"]}
            _worker_call(session,("update",{t["id"]:t for t in tracks},result_cfg))
            session["tracks"],session["mixer"]=tracks,cfg
        return jsonify({"ok":True})


    @app.post("/vst/realtime/editor")
    @endpoint
    def realtime_editor():
        if not request.is_json: _fail("INVALID_REQUEST", "Content-Type must be application/json", 415)
        body = request.get_json(silent=True)
        if not isinstance(body, dict): _fail("INVALID_REQUEST", "JSON object required")
        action = body.get("action")
        if not isinstance(action, str) or action not in {"open", "status", "close"}:
            _fail("INVALID_ACTION", "action must be open, status or close")
        sid = body.get("session_id")
        if not isinstance(sid, str) or not sid: _fail("INVALID_REQUEST", "session_id is required")
        with _sessions_lock: session = _sessions.get(sid)
        if session is None or session["closed"]: _fail("SESSION_NOT_FOUND", "realtime session is closed or unknown", 404)

        target = None
        if action in {"status", "close"}:
            worker_pid = body.get("worker_pid")
            editor_revision = body.get("revision")
            if type(worker_pid) is not int or type(editor_revision) is not int:
                _fail("INVALID_EDITOR_TARGET", "worker_pid and revision are required for editor status/close")
        if action == "open":
            kind = body.get("kind")
            if not isinstance(kind, str) or kind not in {"track", "bus", "master"}:
                _fail("INVALID_EDITOR_TARGET", "kind must be track, bus or master")
            if "index" not in body or type(body["index"]) is not int or body["index"] < 0:
                _fail("INVALID_EDITOR_TARGET", "index must be a non-negative integer")
            index = body["index"]
            if kind == "master":
                if "id" in body and body["id"] != "master": _fail("INVALID_EDITOR_TARGET", "master id must be master")
                target_id = "master"
                effects = session["mixer"]["master"]["effects"]
            elif kind == "track":
                target_id = body.get("id")
                if not isinstance(target_id, str) or not target_id: _fail("INVALID_EDITOR_TARGET", "track id is required")
                track = next((item for item in session["tracks"] if item["id"] == target_id), None)
                if track is None: _fail("EDITOR_TARGET_NOT_FOUND", "track target was not found", 404)
                effects = track["fx"]
            else:
                target_id = body.get("id")
                if not isinstance(target_id, str) or not target_id: _fail("INVALID_EDITOR_TARGET", "bus id is required")
                bus = session["mixer"]["buses_by_id"].get(target_id)
                if bus is None: _fail("EDITOR_TARGET_NOT_FOUND", "bus target was not found", 404)
                effects = bus["effects"]
            if index >= len(effects): _fail("EDITOR_TARGET_NOT_FOUND", "effect index was not found", 404)
            spec = effects[index]
            if spec["type"] != "vst3": _fail("EDITOR_TARGET_NOT_VST3", "selected effect is not VST3", 400)
            supplied_path = body.get("path")
            if supplied_path is None:
                _fail("INVALID_EDITOR_TARGET", "VST3 plugin path is required")
            try:
                actual_path = vst_host.resolve_plugin_path(spec["path"])
                requested_path = vst_host.resolve_plugin_path(supplied_path)
            except (TypeError, ValueError, vst_host.VSTHostError):
                _fail("INVALID_EDITOR_TARGET", "invalid VST3 plugin path")
            if actual_path != requested_path:
                _fail("EDITOR_TARGET_MISMATCH", "plugin path does not match the live mixer effect", 409)
            target = {"kind": kind, "id": target_id, "index": index}

        with session["lock"]:
            if session["closed"]: _fail("SESSION_NOT_FOUND", "realtime session is closed or unknown", 404)
            if action in {"status", "close"}:
                current = _worker_call(session, ("editor_status",))
                if current.get("worker_pid") != worker_pid or current.get("revision") != editor_revision:
                    _fail("EDITOR_REVISION_MISMATCH", "editor session revision changed", 409)
            if action == "open":
                result = _worker_call(session, ("editor_open", target))
                command_error = result.get("command_error") if isinstance(result, dict) else None
                if command_error == "EDITOR_ALREADY_OPEN": _fail("EDITOR_ALREADY_OPEN", "a realtime plugin editor is already open", 409)
                if command_error == "EDITOR_TARGET_NOT_FOUND": _fail("EDITOR_TARGET_NOT_FOUND", "effect target was not found", 404)
                if command_error == "EDITOR_TARGET_NOT_VST3": _fail("EDITOR_TARGET_NOT_VST3", "selected effect is not VST3", 400)
                status_code = 202
            elif action == "status":
                result = _worker_call(session, ("editor_status",))
                status_code = 200
            else:
                result = _worker_call(session, ("editor_close",))
                status_code = 200

        state = result.get("state", "failed") if isinstance(result, dict) else "failed"
        response = {"mode": "live_instance", "session_id": sid, "state": state,
                    "kind": result.get("kind"), "id": result.get("id"), "index": result.get("index"),
                    "path": result.get("path"), "worker_pid": result.get("worker_pid"),
                    "editor_hwnd": result.get("editor_hwnd"), "editor_class": result.get("editor_class"),
                    "revision": result.get("revision")}
        if state == "completed":
            response["parameters"] = result["parameters"]
            response["state_base64"] = result["state_base64"]
        elif state == "failed":
            response["error"] = result.get("error", "native plugin editor failed")
        return jsonify(response), status_code
    @app.post("/vst/realtime/close")
    @endpoint
    def close_session():
        body=request.get_json(silent=True)
        if not isinstance(body,dict): _fail("INVALID_REQUEST","JSON object required")
        sid=body.get("session_id")
        with _sessions_lock: session=_sessions.get(sid)
        if session:
            with session["lock"]:
                if session["closed"]: _fail("SESSION_NOT_FOUND", "realtime session is closed or unknown", 404)
                try:
                    editor = _worker_call(session, ("editor_status",))
                    if editor.get("state") == "opening":
                        final = _worker_call(session, ("editor_close",))
                        if final.get("state") not in {"completed", "failed"}:
                            _fail("EDITOR_CLOSE_UNCONFIRMED", "realtime editor did not return final state before shutdown", 504)
                except SessionError:
                    _terminate(session)
                    with _sessions_lock: _sessions.pop(sid, None)
                    raise
                try: _worker_call(session, ("close",))
                finally:
                    _terminate(session)
                    with _sessions_lock: _sessions.pop(sid, None)
        return jsonify({"ok":True})
