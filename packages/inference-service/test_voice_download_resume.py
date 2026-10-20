"""Real HTTP coverage for interrupted official-archive transfer semantics."""
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from inference_service import voice_install as install


@pytest.fixture
def source(monkeypatch):
    payload = bytes(range(256)) * 8192 + b'archive end'
    requests = []
    mode = {'value': 'resume'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requested = self.headers.get('Range')
            requests.append(requested)
            if requested and mode['value'] != 'ignore':
                start = int(requested.removeprefix('bytes=').removesuffix('-'))
                self.send_response(206)
                reported = start + 1 if mode['value'] == 'wrong' else start
                self.send_header('Content-Range', f'bytes {reported}-{len(payload)-1}/{len(payload)}')
                self.send_header('Content-Length', str(len(payload) - start))
                self.end_headers()
                self.wfile.write(payload[start:])
            else:
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload[:1048576] if mode['value'] == 'interrupt' else payload)
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    item = {'id': 'fixture', 'source_url': f'http://127.0.0.1:{server.server_port}/archive',
            'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    monkeypatch.setattr(install, '_JOB', {})
    try:
        yield item, payload, requests, mode
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_interrupted_transfer_retains_prefix_and_resumes(source, tmp_path):
    item, payload, requests, mode = source
    target = tmp_path / 'archive.part'
    mode['value'] = 'interrupt'
    with pytest.raises(ValueError):
        install._download(item, target)
    assert target.read_bytes() == payload[:1048576]
    mode['value'] = 'resume'
    install._download(item, target)
    assert requests == [None, 'bytes=1048576-']
    assert target.read_bytes() == payload


def test_ignored_range_replaces_instead_of_appending(source, tmp_path):
    item, payload, requests, mode = source
    target = tmp_path / 'archive.part'
    target.write_bytes(payload[:12345])
    mode['value'] = 'ignore'
    install._download(item, target)
    assert requests == ['bytes=12345-']
    assert target.read_bytes() == payload


def test_wrong_range_does_not_change_saved_prefix(source, tmp_path):
    item, payload, requests, mode = source
    target = tmp_path / 'archive.part'
    prefix = payload[:12345]
    target.write_bytes(prefix)
    mode['value'] = 'wrong'
    with pytest.raises(ValueError):
        install._download(item, target)
    assert target.read_bytes() == prefix


def test_complete_cache_is_verified_without_network_and_corrupt_cache_removed(source, tmp_path):
    item, payload, requests, mode = source
    target = tmp_path / 'archive.part'
    target.write_bytes(payload)
    install._download(item, target)
    assert requests == []
    target.write_bytes(b'x' * len(payload))
    with pytest.raises(ValueError, match='SHA-256'):
        install._download(item, target)
    assert not target.exists()
    assert requests == []
