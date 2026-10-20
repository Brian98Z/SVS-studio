"""Single isolated native plugin editor; parameters return only after closing."""
from __future__ import annotations

import atexit
import multiprocessing as mp
import threading
import time
import uuid

from flask import jsonify, request

from . import vst_host

_LOCK = threading.RLock()
_SESSION = None


def _editor_main(conn, path, parameters, state_base64=None):
    close = threading.Event()
    stopped = threading.Event()
    def watch():
        deadline = time.monotonic() + 30
        try:
            while not stopped.wait(.1):
                if conn.poll():
                    if conn.recv() == 'close':
                        close.set()
                        return
                    deadline = time.monotonic() + 30
                if time.monotonic() > deadline:
                    close.set()
                    return
        except (EOFError, OSError):
            close.set()
    try:
        plugin = vst_host.open_effect(path)
        vst_host.restore_plugin_state(plugin, state_base64)
        for name, value in parameters.items():
            vst_host.set_parameter(plugin, name, value)
        conn.send({'state': 'opening'})
        monitor = threading.Thread(target=watch, daemon=True)
        monitor.start()
        plugin.show_editor(close)
        stopped.set()
        monitor.join(1)
        conn.send({'state': 'completed', 'parameters': vst_host.enumerate_parameters(plugin),
                   'state_base64': vst_host.capture_plugin_state(plugin)})
    except BaseException:
        try:
            conn.send({'state': 'failed', 'error': '原生插件编辑器无法打开或意外退出'})
        except (OSError, EOFError):
            pass
    finally:
        stopped.set()
        conn.close()


def _stop(session):
    proc = session['proc']
    if proc.is_alive():
        try:
            session['conn'].send('close')
        except (OSError, EOFError):
            pass
        proc.join(3)
        if proc.is_alive():
            proc.terminate()
            proc.join(1)
    session['conn'].close()


def _cleanup():
    with _LOCK:
        if _SESSION:
            _stop(_SESSION)


atexit.register(_cleanup)


def _status(session):
    if session['result']['state'] not in {'completed', 'failed'} and session.get('closing_at') is not None and time.monotonic() - session['closing_at'] > 5:
        _stop(session)
        session['result'] = {'state': 'failed', 'error': '原生编辑器关闭超时，进程已释放，未应用参数'}
    if session['result']['state'] not in {'completed', 'failed'}:
        try:
            while session['conn'].poll():
                session['result'] = session['conn'].recv()
            if session['result']['state'] not in {'completed', 'failed'}:
                if session['proc'].is_alive():
                    session['conn'].send('ping')
                else:
                    session['result'] = {'state': 'failed', 'error': '原生编辑器进程已退出，未应用参数'}
        except (OSError, EOFError):
            if session['result']['state'] not in {'completed', 'failed'}:
                session['result'] = {'state': 'failed', 'error': '原生编辑器连接已断开，未应用参数'}
        if session['result']['state'] in {'completed', 'failed'}:
            _stop(session)
    return {'id': session['id'], 'path': session['path'], **session['result']}


def _reap():
    while True:
        time.sleep(1)
        with _LOCK:
            if _SESSION and time.monotonic() - _SESSION['last_poll'] > 30:
                if _SESSION['result']['state'] not in {'completed', 'failed'}:
                    _stop(_SESSION)
                    _SESSION['result'] = {'state': 'failed', 'error': '编辑器连接超时，进程已释放，未应用参数'}
            elif _SESSION and _SESSION.get('closing_at') is not None:
                _status(_SESSION)


threading.Thread(target=_reap, name='vst-editor-reaper', daemon=True).start()


def register_routes(app):
    @app.post('/vst/editor')
    def editor():
        global _SESSION
        if not request.is_json:
            return jsonify({'detail': 'Content-Type must be application/json'}), 415
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({'detail': 'JSON object required'}), 400
        with _LOCK:
            action = body.get('action')
            if action == 'open':
                if _SESSION and _status(_SESSION)['state'] not in {'completed', 'failed'}:
                    return jsonify({'detail': '已有原生编辑器打开，请先关闭'}), 409
                try:
                    path = str(vst_host._validated_bundle(body.get('path', '')))
                    parameters = vst_host._validate_parameters(body.get('parameters', {}))
                    state_base64 = vst_host.validate_state(body.get('state_base64'))
                except (ValueError, TypeError, vst_host.VSTHostError) as exc:
                    return jsonify({'detail': str(exc)}), 400
                if _SESSION:
                    _stop(_SESSION)
                context = mp.get_context('spawn')
                parent, child = context.Pipe()
                proc = context.Process(target=_editor_main, args=(child, path, parameters, state_base64), daemon=True)
                try:
                    proc.start()
                except Exception:
                    parent.close()
                    child.close()
                    return jsonify({'detail': '无法启动隔离编辑器进程'}), 422
                child.close()
                _SESSION = {'id': uuid.uuid4().hex, 'path': path, 'proc': proc, 'conn': parent,
                            'result': {'state': 'opening'}, 'last_poll': time.monotonic(), 'closing_at': None}
                return jsonify(_status(_SESSION)), 202
            if action not in {'status', 'close'}:
                return jsonify({'detail': 'action must be open, status or close'}), 400
            if not _SESSION or body.get('id') != _SESSION['id']:
                return jsonify({'detail': 'editor session not found'}), 404
            _SESSION['last_poll'] = time.monotonic()
            if action == 'close' and _SESSION['result']['state'] not in {'completed', 'failed'}:
                if _SESSION['closing_at'] is None:
                    _SESSION['closing_at'] = time.monotonic()
                try:
                    _SESSION['conn'].send('close')
                except (OSError, EOFError):
                    pass
            return jsonify(_status(_SESSION))
