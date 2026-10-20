"""Focused regressions for filesystem-only discovery and safe VST API contracts."""
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from inference_service import vst_host  # noqa: E402


def test_discovery_recurses_vendors_and_does_not_descend_into_bundles(tmp_path):
    root = tmp_path / "VST3"
    nested = root / "Vendor" / "Collection"
    nested.mkdir(parents=True)
    binary = nested / "Single.vst3"
    binary.write_bytes(b"not executed")
    bundle = nested / "Bundle.vst3"
    (bundle / "Contents" / "x86_64-win").mkdir(parents=True)
    (bundle / "Contents" / "x86_64-win" / "Bundle.vst3").write_bytes(b"inside")
    result = vst_host.discover_vst3([root])
    assert {item["name"] for item in result} == {"Single.vst3", "Bundle.vst3"}
    assert all(item["path"] != str(bundle / "Contents" / "x86_64-win" / "Bundle.vst3") for item in result)


def test_validated_path_resolves_project_plugins_and_rejects_escape(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("SVS_PROJECT_ROOT", str(tmp_path))
    binary = root / "plugin.vst3"
    binary.write_bytes(b"plugin")
    bundle = root / "bundle.vst3"
    bundle.mkdir()
    (bundle / "binary").write_bytes(b"plugin")
    assert vst_host._validated_bundle("plugins/plugin.vst3") == binary.resolve()
    assert vst_host._validated_bundle("plugin.vst3") == binary.resolve()
    assert vst_host._validated_bundle(str(bundle)) == bundle.resolve()
    with pytest.raises(vst_host.VSTHostError, match="inside"):
        vst_host._validated_bundle("../outside.vst3")


class FakePlugin:
    def __init__(self):
        self.parameters = {
            "Gain": SimpleNamespace(min_value=0.0, max_value=2.0, step=0.1, type=float),
            "Mode": SimpleNamespace(valid_values=["Clean", "Bright"], type=str),
            "Enabled": SimpleNamespace(type=bool),
        }
        self.Gain = 0.5
        self.Mode = "Clean"
        self.Enabled = False


def test_parameters_read_values_from_plugin_and_expose_metadata():
    plugin = FakePlugin()
    params = {item["name"]: item for item in vst_host.enumerate_parameters(plugin)}
    assert params["Gain"]["value"] == 0.5
    assert params["Gain"]["min_value"] == 0.0 and params["Gain"]["max_value"] == 2.0
    assert params["Gain"]["step"] == 0.1 and params["Gain"]["type"] == "number"
    assert params["Mode"]["valid_values"] == ["Clean", "Bright"]


def test_set_parameter_handles_scalars_and_actual_readback():
    plugin = FakePlugin()
    assert vst_host.set_parameter(plugin, "Gain", 1.25) == 1.25
    assert vst_host.set_parameter(plugin, "Mode", "Bright") == "Bright"
    assert vst_host.set_parameter(plugin, "Enabled", True) is True
    with pytest.raises(vst_host.PluginParameterError, match="range"):
        vst_host.set_parameter(plugin, "Gain", 3.0)
    with pytest.raises(vst_host.PluginParameterError, match="not valid"):
        vst_host.set_parameter(plugin, "Mode", "Unknown")


def _preset(class_id=b"c" * 32, body=b"state"):
    list_offset = 48 + len(body)
    return (b"VST3" + (1).to_bytes(4, "little") + class_id + list_offset.to_bytes(8, "little")
            + body + b"List" + (1).to_bytes(4, "little") + b"Comp" + (48).to_bytes(8, "little")
            + len(body).to_bytes(8, "little"))


def test_state_validation_rejects_malformed_and_accepts_absent_or_valid():
    import base64
    valid = base64.b64encode(_preset()).decode("ascii")
    assert vst_host.validate_state(None) is None
    assert vst_host.validate_state(valid) == valid
    truncated = _preset()[:-1]
    outside = bytearray(_preset())
    outside[-16:-8] = (99999).to_bytes(8, "little")
    for malformed in ("not base64!", base64.b64encode(b"short").decode("ascii"),
                      base64.b64encode(truncated).decode("ascii"), base64.b64encode(outside).decode("ascii"),
                      base64.b64encode(b"NOPE" + b"\0" * 44).decode("ascii")):
        with pytest.raises(vst_host.VSTHostError):
            vst_host.validate_state(malformed)


def test_restore_rejects_foreign_plugin_class_without_mutating_state():
    import base64
    class StatefulPlugin(FakePlugin):
        preset_data = _preset()
    plugin = StatefulPlugin()
    before = plugin.preset_data
    wrong_class = base64.b64encode(_preset(class_id=b"x" * 32)).decode("ascii")
    with pytest.raises(vst_host.VSTHostError, match="class ID"):
        vst_host.restore_plugin_state(plugin, wrong_class)
    assert plugin.preset_data == before


def test_state_capture_requires_native_preset_api():
    with pytest.raises(vst_host.VSTHostError, match="not supported"):
        vst_host.capture_plugin_state(FakePlugin())

def test_process_validates_before_starting_native_worker(tmp_path, monkeypatch):
    binary = tmp_path / "plugin.vst3"
    binary.write_bytes(b"placeholder")
    monkeypatch.setattr(vst_host, "_run_worker", lambda *args, **kwargs: pytest.fail("worker started"))
    with pytest.raises(vst_host.VSTHostError, match="tail_seconds"):
        vst_host.process_effect(str(binary), np.zeros((2, 8), np.float32), 48000, tail_seconds=10.1)
    with pytest.raises(vst_host.VSTHostError, match="finite"):
        vst_host.process_effect(str(binary), np.zeros((2, 8), np.float32), float("nan"))




@pytest.mark.parametrize('latency', [0, 2, 13])
@pytest.mark.parametrize('omit_startup', [False, True])
def test_offline_latency_aligns_start_and_keeps_last_sample(latency, omit_startup):
    class DelayedPlugin:
        reported_latency_samples = latency

        def __init__(self):
            self.saved = np.zeros((2, latency), np.float32)
            self.omit = latency if omit_startup else 0

        def process(self, audio, *, sample_rate, reset, buffer_size):
            joined = np.concatenate((self.saved, audio), axis=1)
            rendered = joined[:, :audio.shape[1]]
            self.saved = joined[:, audio.shape[1]:]
            count = min(self.omit, rendered.shape[1])
            self.omit -= count
            return rendered[:, count:]

    source = np.array([[1, 0, 2, 0, 0, 0, 0, 3], [-1, 0, -2, 0, 0, 0, 0, -3]], np.float32)
    result = vst_host.process_stereo(DelayedPlugin(), source, 48000, block_frames=4)
    np.testing.assert_array_equal(result, source)


def test_changing_offline_latency_fails_instead_of_misaligning_audio():
    class ChangingPlugin:
        reported_latency_samples = 2

        def process(self, audio, **kwargs):
            self.reported_latency_samples += 1
            return audio

    with pytest.raises(vst_host.VSTHostError, match='latency changed'):
        vst_host.process_stereo(ChangingPlugin(), np.ones((2, 8), np.float32), 48000, block_frames=4)
