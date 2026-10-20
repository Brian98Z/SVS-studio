import time
from types import SimpleNamespace
from flask import Flask
import pytest

from inference_service import vst_editor as editor


def test_editor_rejects_non_json_and_unknown_sessions_without_loading(monkeypatch):
    monkeypatch.setattr(editor, '_SESSION', None)
    app = Flask(__name__)
    editor.register_routes(app)
    client = app.test_client()
    assert client.post('/vst/editor', data='{}').status_code == 415
    assert client.post('/vst/editor', json=[]).status_code == 400
    assert client.post('/vst/editor', json={'action':'status','id':'missing'}).status_code == 404
    assert client.post('/vst/editor', json={'action':'open','path':'../outside.vst3'}).status_code == 400


def test_failed_editor_never_returns_or_applies_partial_parameters(monkeypatch):
    class BrokenPlugin:
        def show_editor(self, event): raise RuntimeError('secret path or plugin failure')
    messages = []
    class Connection:
        def send(self, value): messages.append(value)
        def poll(self): return False
        def close(self): pass
    monkeypatch.setattr(editor.vst_host, 'open_effect', lambda path: BrokenPlugin())
    editor._editor_main(Connection(), 'fixture', {})
    assert messages[-1] == {'state':'failed','error':'原生插件编辑器无法打开或意外退出'}
    assert 'parameters' not in messages[-1]


def test_close_deadline_releases_hung_plugin_and_does_not_report_success():
    class Process:
        alive = True
        def is_alive(self): return self.alive
        def join(self, timeout): pass
        def terminate(self): self.alive = False
    class Connection:
        def send(self, value): pass
        def close(self): pass
    proc = Process()
    session = {'id':'id','path':'fixture','proc':proc,'conn':Connection(),
               'closing_at':time.monotonic()-6,'result':{'state':'opening'}}
    result = editor._status(session)
    assert not proc.is_alive()
    assert result['state'] == 'failed'
    assert 'parameters' not in result


