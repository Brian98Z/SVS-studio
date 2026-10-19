"""Focused voice registry tests; run with pytest test_voices.py from this package."""
import json

import pytest

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
