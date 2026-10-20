import stat
import zipfile
import hashlib
import io
from types import SimpleNamespace
from flask import Flask
import pytest
from inference_service import voice_install as install


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../escape', 'a\\escape', 'C:/escape', 'a/CON.txt', 'a/file.', 'a//file'])
def test_rejects_unsafe_members_before_any_write(tmp_path, name):
    archive = tmp_path / 'bad.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('safe.txt', b'valid')
        z.writestr(name, b'bad')
    if "\\" in name:
        archive.write_bytes(archive.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
    output = tmp_path / 'out'
    output.mkdir()
    with pytest.raises(ValueError):
        install._extract(archive, output)
    assert list(output.iterdir()) == []


def test_rejects_symlink_case_collision_and_file_parent(tmp_path):
    link = zipfile.ZipInfo('link')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    for index, entries in enumerate([[(link, b'outside')], [('Bank/a', b'a'), ('bank/A', b'b')], [('bank', b'x'), ('bank/file', b'y')]]):
        archive = tmp_path / f'{index}.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for name, data in entries:
                z.writestr(name, data)
        output = tmp_path / f'out{index}'
        output.mkdir()
        with pytest.raises(ValueError):
            install._extract(archive, output)
        assert not list(output.iterdir())


def test_preserves_all_original_files(tmp_path):
    files = {'bank/model.onnx': b'original model bytes', 'bank/Terms.pdf': b'terms', 'bank/image.png': b'image', 'notice.txt': b'author notice'}
    archive = tmp_path / 'bank.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    output = tmp_path / 'out'
    output.mkdir()
    install._extract(archive, output)
    assert {str(p.relative_to(output)).replace('\\', '/'): p.read_bytes() for p in output.rglob('*') if p.is_file()} == files


def test_install_requires_consent_reviewed_catalog_and_no_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv('SVS_VOICE_INSTALL_DIR', str(tmp_path))
    monkeypatch.setattr(install, '_JOB', None)
    app = Flask(__name__)
    install.register_routes(app)
    client = app.test_client()
    assert client.post('/voices/install', data='x').status_code == 415
    assert client.post('/voices/install', json={'catalog_id':'qixuan-2.7.0'}).status_code == 422
    assert client.post('/voices/install', json={'catalog_id':'https://untrusted/', 'accepted_terms':True}).status_code == 422
    existing = tmp_path / 'qixuan-2.7.0'
    existing.mkdir()
    sentinel = existing / 'user-data'
    sentinel.write_bytes(b'keep')
    assert client.post('/voices/install', json={'catalog_id':'qixuan-2.7.0', 'accepted_terms':True}).status_code == 409
    assert sentinel.read_bytes() == b'keep'


def test_download_rejects_corruption_and_truncation(tmp_path, monkeypatch):
    class Response(io.BytesIO):
        status = 200
        headers = {}

    original = b'author archive bytes'
    item = {'source_url': 'https://github.com/owner/repo/archive.zip', 'bytes': len(original), 'sha256': hashlib.sha256(original).hexdigest()}
    monkeypatch.setattr(install, '_JOB', {})
    for index, payload in enumerate([original[:-1], b'x' * len(original)]):
        monkeypatch.setattr(install.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=lambda *args, **kwargs: Response(payload)))
        with pytest.raises(ValueError, match='SHA-256'):
            install._download(item, tmp_path / f'bad{index}.zip')


def test_concurrent_registration_preserves_both_entries(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    from inference_service import voices
    monkeypatch.setenv('SVS_VOICE_REGISTRY', str(tmp_path / 'registry.json'))
    banks = [tmp_path / 'a', tmp_path / 'b']
    for bank in banks:
        bank.mkdir()
    barrier = threading.Barrier(2)
    original_inspect = voices.inspect_voice
    original_save = voices._save
    def inspect(*args):
        result = original_inspect(*args)
        barrier.wait(timeout=5)
        return result
    def save(entries):
        time.sleep(.02)
        original_save(entries)
    monkeypatch.setattr(voices, 'inspect_voice', inspect)
    monkeypatch.setattr(voices, '_save', save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda bank: voices.register_voice(str(bank)), banks))
    assert {v['id'] for v in voices._load()} == {v['id'] for v in results}


def test_cleanup_failure_does_not_leave_dangling_registration(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from inference_service import voices
    monkeypatch.setenv('SVS_VOICE_REGISTRY', str(tmp_path / 'voices.json'))
    monkeypatch.setattr(install, '_JOB', {})
    original_temporary = install.tempfile.TemporaryDirectory
    @contextmanager
    def failing_cleanup(*args, **kwargs):
        with original_temporary(*args, **kwargs) as stage:
            yield stage
        raise PermissionError('staging cleanup denied')
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('bank/dsconfig.yaml', 'acoustic: model.onnx')
        z.writestr('bank/Terms_of_Use.zh-CN.pdf', b'fixture terms')
    item = {'id': 'fixture', 'name': 'Fixture', 'version': '1', 'source_url': 'https://github.com/fixture', 'bytes': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    class Response(io.BytesIO):
        status = 200
        headers = {}
    monkeypatch.setattr(install.urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=lambda *args, **kwargs: Response(archive.read_bytes())))
    monkeypatch.setattr(install.tempfile, 'TemporaryDirectory', failing_cleanup)
    install._run(item, tmp_path / 'installed')
    assert install._snapshot()['job']['state'] == 'failed'
    assert voices._load() == []
    assert not (tmp_path / 'installed/fixture').exists()
    assert not list((tmp_path / 'installed').glob('.voice-install-*'))
    assert install._package_path(item, tmp_path / 'installed').read_bytes() == archive.read_bytes()
