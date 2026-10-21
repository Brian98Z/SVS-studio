"""Focused voice registry tests; run with pytest test_voices.py from this package."""
import json

import pytest
import yaml

from inference_service import voices


def test_registration_is_persisted_and_removal_keeps_assets(tmp_path, monkeypatch):
    registry = tmp_path / "user" / "voices.json"
    bank = tmp_path / "bank"
    bank.mkdir()
    model = bank / "voice.onnx"
    model.write_bytes(b"test model placeholder")
    monkeypatch.setenv("SVS_VOICE_REGISTRY", str(registry))

    first = voices.inspect_voice(str(bank))
    second = voices.inspect_voice(str(bank))
    assert first["id"] == second["id"]
    app = __import__("flask").Flask(__name__)
    voices.register_routes(app)
    client = app.test_client()
    response = client.post("/voices", json={"path": str(bank)})
    assert response.status_code == 201
    voice_id = response.json["id"]
    assert voices.resolve_voice(voice_id)["registered"]
    assert json.loads(registry.read_text(encoding="utf-8"))["voices"][0]["id"] == voice_id

    removed = client.delete(f"/voices/{voice_id}")
    assert removed.json["assets_deleted"] is False
    assert model.is_file()
    assert voices.resolve_voice(voice_id) is None


def test_nonexistent_bank_rejected(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        voices.inspect_voice(str(tmp_path / "missing"))


def test_invalid_blend_weights_rejected():
    for weights in ([float("nan")], [0], [-0.1], [1.1], [0.2]):
        with pytest.raises(ValueError):
            voices.validate_speaker_blend(["voice.unknown"], weights)

@pytest.fixture
def vocoder_pair(tmp_path, monkeypatch):
    bank, vocoder = tmp_path / 'bank', tmp_path / 'vocoder'
    bank.mkdir()
    vocoder.mkdir()
    settings = dict(sample_rate=44100, hop_size=512, win_size=2048, fft_size=2048,
                    num_mel_bins=128, mel_fmin=40, mel_fmax=16000, mel_base='e', mel_scale='slaney')
    (bank / 'dsconfig.yaml').write_text(yaml.safe_dump({**settings, 'vocoder': 'selected', 'acoustic': 'missing.onnx'}))
    (vocoder / 'vocoder.yaml').write_text(yaml.safe_dump({**settings, 'name': 'selected', 'model': 'selected.onnx'}))
    (vocoder / 'selected.onnx').write_bytes(b'not executed by metadata validation')
    monkeypatch.setenv('SVS_VOICE_REGISTRY', str(tmp_path / 'registry.json'))
    app = __import__('flask').Flask(__name__)
    voices.register_routes(app)
    descriptor = voices.register_voice(str(bank))
    return bank, vocoder, app.test_client(), descriptor['id']


def test_vocoder_binding_survives_reregistration_and_clear_keeps_assets(vocoder_pair):
    bank, vocoder, client, voice_id = vocoder_pair
    endpoint = f'/voices/{voice_id}/vocoder'
    assert client.patch(endpoint, json={'path': str(vocoder)}).status_code == 400
    response = client.patch(endpoint, json={'path': str(vocoder), 'acknowledged_license': True})
    assert response.status_code == 200
    assert response.json['vocoder']['source'] == 'external'
    assert response.json['vocoder']['compatible']
    # A compatible vocoder alone cannot make a missing acoustic model ready.
    assert not response.json['inference_ready']
    assert voices.register_voice(str(bank))['vocoder_path'] == str(vocoder.resolve())
    cleared = client.patch(endpoint, json={'path': None})
    assert cleared.status_code == 200
    assert cleared.json['vocoder_path'] is None
    assert cleared.json['vocoder']['source'] == 'missing'
    assert (vocoder / 'selected.onnx').is_file()
    assert not (bank / 'dsvocoder').exists()


@pytest.mark.parametrize('change', ['missing_reference', 'wrong_hop', 'escape', 'boolean_rate'])
def test_incompatible_vocoder_refusal_keeps_binding(vocoder_pair, change):
    bank, vocoder, client, voice_id = vocoder_pair
    endpoint = f'/voices/{voice_id}/vocoder'
    payload = {'path': str(vocoder), 'acknowledged_license': True}
    assert client.patch(endpoint, json=payload).status_code == 200
    registry_before = voices._config_path().read_bytes()
    path = vocoder / 'vocoder.yaml'
    cfg = yaml.safe_load(path.read_text())
    if change == 'missing_reference':
        cfg['model'] = 'missing.onnx'  # An unrelated ONNX still exists.
    elif change == 'wrong_hop':
        cfg['hop_size'] = 256
    elif change == 'escape':
        cfg['model'] = '../outside.onnx'
        (vocoder.parent / 'outside.onnx').write_bytes(b'outside')
    else:
        cfg['sample_rate'] = True
    path.write_text(yaml.safe_dump(cfg))
    assert client.patch(endpoint, json=payload).status_code == 400
    assert voices._config_path().read_bytes() == registry_before
    assert not voices.resolve_voice(voice_id)['vocoder']['compatible']


@pytest.mark.parametrize('body', [{}, [], {'path': ''}, {'path': 7}])
def test_invalid_vocoder_request_never_clears_binding(vocoder_pair, body):
    bank, vocoder, client, voice_id = vocoder_pair
    endpoint = f'/voices/{voice_id}/vocoder'
    assert client.patch(endpoint, json={'path': str(vocoder), 'acknowledged_license': True}).status_code == 200
    before = voices._config_path().read_bytes()
    assert client.patch(endpoint, json=body).status_code == 400
    assert client.patch(endpoint, data='{}', content_type='text/plain').status_code == 400
    assert voices._config_path().read_bytes() == before


def _valid_dsconfigs(bank, acoustic_ref='acoustic.onnx'):
    (bank / 'dsconfig.yaml').write_text(yaml.safe_dump({'acoustic': acoustic_ref}))
    for component, key in (('dsdur', 'dur'), ('dspitch', 'pitch'), ('dsvariance', 'variance')):
        directory = bank / component
        directory.mkdir(exist_ok=True)
        (directory / 'dsconfig.yaml').write_text(yaml.safe_dump({'linguistic': '../ling.onnx', key: '../' + component + '.onnx'}))


@pytest.mark.parametrize('reference', [r'\\server\share\model.onnx', r'\\?\C:\model.onnx', r'C:\model.onnx', r'C:model.onnx', '/model.onnx', '../outside.onnx'])
def test_dsconfig_refs_reject_network_paths_before_probe_or_predictor_import(tmp_path, monkeypatch, reference):
    from pathlib import Path
    import builtins
    from inference_service import vocoder_binding

    bank = tmp_path / 'bank'
    bank.mkdir()
    _valid_dsconfigs(bank, reference)
    probed = []
    original_is_file = Path.is_file

    def guarded_is_file(path):
        probed.append(str(path))
        return original_is_file(path)

    monkeypatch.setattr(Path, 'is_file', guarded_is_file)
    with pytest.raises(ValueError, match='non-local|escapes voicebank'):
        vocoder_binding.validate_dsconfig_references(bank)
    assert not probed

    real_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith('diffsinger_utau'):
            raise AssertionError('external predictor import happened before validation')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    with pytest.raises(ValueError, match='non-local|escapes voicebank'):
        vocoder_binding.build_predictor(bank, None)


def test_dsconfig_refs_allow_relative_references_within_bank(tmp_path):
    from inference_service.vocoder_binding import validate_dsconfig_references

    bank = tmp_path / 'bank'
    bank.mkdir()
    _valid_dsconfigs(bank)
    for name in ('acoustic.onnx', 'ling.onnx', 'dsdur.onnx', 'dspitch.onnx', 'dsvariance.onnx'):
        (bank / name).write_bytes(b'fixture')
    assert validate_dsconfig_references(bank) == []


def test_asset_revision_rejects_old_memory_and_disk_audio(tmp_path, monkeypatch):
    import numpy as np
    from inference_service import cache_manager
    monkeypatch.setattr(cache_manager, 'DISK_DIR', tmp_path)
    cache = cache_manager.AudioCache()
    audio = np.asarray([.2, -.1, .3], dtype=np.float32)
    cache.put('phrase', audio, revision='vocoder-a')
    np.testing.assert_array_equal(cache.get('phrase', revision='vocoder-a'), audio)
    assert cache.get('phrase', revision='vocoder-b') is None
    restarted = cache_manager.AudioCache()
    assert restarted.get('phrase', revision='vocoder-b') is None
    np.testing.assert_array_equal(restarted.get('phrase', revision='vocoder-a'), audio)
    replacement = -audio
    restarted.put('phrase', replacement, revision='vocoder-b')
    assert restarted.get('phrase', revision='vocoder-a') is None
    np.testing.assert_array_equal(restarted.get('phrase', revision='vocoder-b'), replacement)
    assert restarted.invalidate(['phrase']) == 1  # Public request hash is retained.
