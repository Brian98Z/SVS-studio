from types import SimpleNamespace

import pytest

from inference_service import hardware
from diffsinger_utau.voice_bank.commons.voice_bank_reader import OnnxReader


class _Session:
    def __init__(self, providers):
        self.providers = providers

    def get_providers(self):
        return self.providers

    def get_inputs(self):
        return [SimpleNamespace(name="features", shape=[1, 8])]

    def get_outputs(self):
        return [SimpleNamespace(name="result", shape=[1, 2])]


def test_unknown_preference_is_rejected():
    with pytest.raises(ValueError, match="Unknown inference device preference"):
        hardware.setup_runtime(object(), "npu")


def test_auto_uses_only_loaded_provider_and_preserves_reader(monkeypatch, tmp_path):
    import onnxruntime as ort
    model = tmp_path / "model.onnx"
    model.write_bytes(b"onnx")
    reader = OnnxReader(model)
    pred = SimpleNamespace(dsacoustic=SimpleNamespace(acoustic_model=reader))
    monkeypatch.setattr(ort, "get_available_providers", lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
    calls = []

    def create(path, sess_options=None, providers=None):
        calls.append(providers)
        if providers[0] == "CUDAExecutionProvider":
            raise RuntimeError("CUDA driver unavailable")
        return _Session(["CPUExecutionProvider"])

    monkeypatch.setattr(ort, "InferenceSession", create)
    report = hardware.setup_runtime(pred, "auto")
    assert calls == [["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]]
    assert report["sessions"][0]["provider"] == "CPUExecutionProvider"
    assert reader.session.get_providers() == ["CPUExecutionProvider"]
    assert reader.input_names == ["features"]
    assert reader.input_shapes == {"features": [1, 8]}


def test_explicit_provider_failure_does_not_silently_fallback(monkeypatch, tmp_path):
    import onnxruntime as ort
    path = tmp_path / "model.onnx"
    path.write_bytes(b"onnx")
    reader = OnnxReader(path)
    pred = SimpleNamespace(dsvocoder=SimpleNamespace(model=reader))
    monkeypatch.setattr(ort, "get_available_providers", lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(ort, "InferenceSession", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("missing CUDA")))
    with pytest.raises(RuntimeError, match="No requested inference provider loaded"):
        hardware.setup_runtime(pred, "cuda")
    assert reader.session is None


def test_hardware_route_includes_status(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    hardware.register_routes(app)
    monkeypatch.setattr(hardware, "runtime_status", lambda: {"preference": "cpu", "sessions": []})
    response = app.test_client().get("/hardware")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "preference": "cpu", "sessions": []}
    invalid = app.test_client().get("/hardware?device=npu")
    assert invalid.status_code == 422
