"""Regression coverage for deterministic stereo mixer behavior and routing."""
from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from inference_service.mixer import (  # noqa: E402
    default_mixer_config, mix_tracks, pan_stereo, process_effects, validate_config,
)


def test_default_config_is_serializable_and_bypassed():
    import json
    config = default_mixer_config()
    assert json.loads(json.dumps(config)) == config
    audio = np.array([.2, -.1, .4], dtype=np.float32)
    out = mix_tracks([{"id": "a", "audio": audio}], 24000, config)
    np.testing.assert_allclose(out[:, 0], audio / np.sqrt(2), atol=1e-7)
    np.testing.assert_allclose(out[:, 1], audio / np.sqrt(2), atol=1e-7)


def test_equal_power_pan_endpoints_and_center():
    mono = np.ones(32, np.float32)
    left = pan_stereo(mono, -1)
    center = pan_stereo(mono, 0)
    right = pan_stereo(mono, 1)
    assert np.all(left[:, 1] == 0) and np.all(left[:, 0] == 1)
    np.testing.assert_allclose(center[:, 0], 1 / np.sqrt(2), atol=1e-7)
    np.testing.assert_allclose(center[:, 1], 1 / np.sqrt(2), atol=1e-7)
    assert np.all(right[:, 0] == 0) and np.all(right[:, 1] == 1)


def test_pan_rejects_out_of_range_and_invalid_shapes():
    with pytest.raises(ValueError): pan_stereo(np.ones(4), 1.1)
    with pytest.raises(ValueError): pan_stereo(np.ones((4, 3)), 0)


def test_eq_bypass_is_bit_exact_and_enabled_eq_changes_signal():
    impulse = np.zeros((2048, 2), np.float32); impulse[0] = 1
    bypass = [{"type": "eq3", "enabled": False, "low_db": 12, "mid_db": 0, "high_db": 0,
               "low_hz": 250, "high_hz": 4000}]
    assert np.array_equal(process_effects(impulse, bypass, 24000), impulse)
    enabled = [{**bypass[0], "enabled": True}]
    assert not np.array_equal(process_effects(impulse, enabled, 24000), impulse)


def test_reverb_adds_deterministic_tail():
    impulse = np.zeros((12000, 2), np.float32); impulse[0] = 1
    effect = [{"type": "reverb", "enabled": True, "wet": .5, "room": .5}]
    first = process_effects(impulse, effect, 24000)
    second = process_effects(impulse, effect, 24000)
    assert np.array_equal(first, second)
    assert np.any(first[1000:, :] != 0)


def test_compressor_reduces_over_threshold_peak():
    audio = np.ones((12000, 2), np.float32) * .9
    effect = [{"type": "compressor", "enabled": True, "threshold_db": -18,
               "ratio": 4, "attack_ms": 1, "release_ms": 100}]
    out = process_effects(audio, effect, 24000)
    assert float(np.max(np.abs(out[-100:]))) < .9
    assert np.array_equal(process_effects(audio, [{**effect[0], "enabled": False}], 24000), audio)


def test_bus_send_and_output_accumulate_and_process_chain():
    cfg = {"master": {"volume": 1, "effects": []}, "buses": [
        {"id": "verb", "output": "master", "volume": 1, "effects": [], "sends": []}]}
    mono = np.ones(12, np.float32)
    out = mix_tracks([{"id": "v", "audio": mono, "bus": "master",
                       "sends": [{"to": "verb", "level": .25}]}], 24000, cfg)
    expected = 1.25 / np.sqrt(2)
    np.testing.assert_allclose(out, expected, atol=1e-6)


def test_routing_rejects_unknown_destinations_cycles_and_bad_effect_values():
    with pytest.raises(ValueError, match="unknown bus output"):
        validate_config({"master": {}, "buses": [{"id": "a", "output": "missing"}]})
    with pytest.raises(ValueError, match="cycle"):
        validate_config({"master": {}, "buses": [
            {"id": "a", "output": "b"}, {"id": "b", "output": "a"}]})
    with pytest.raises(ValueError, match="EQ low"):
        validate_config({"master": {"effects": [{"type": "eq3", "low_db": 90}]}})

def test_stereo_input_remains_stereo_and_balance_pan_is_deterministic():
    stereo = np.column_stack((np.ones(12, np.float32), np.full(12, .25, np.float32)))
    center = pan_stereo(stereo, 0)
    np.testing.assert_array_equal(center, stereo)
    left = pan_stereo(stereo, -1)
    np.testing.assert_array_equal(left[:, 0], stereo[:, 0])
    np.testing.assert_array_equal(left[:, 1], np.zeros(12, np.float32))


def test_mix_route_accepts_stereo_pcm_and_returns_interleaved_stereo():
    import base64
    from flask import Flask
    from inference_service.mixer import register_routes
    app = Flask(__name__)
    register_routes(app)
    pcm = np.array([1, .5, .25, -.25], dtype='<f4').tobytes()
    response = app.test_client().post('/mix', json={
        'sample_rate': 24000,
        'tracks': [{'id': 'stereo', 'audio': base64.b64encode(pcm).decode(), 'channels': 2}],
    })
    assert response.status_code == 200, response.json
    data = response.json
    assert data['channels'] == 2 and data['frames'] == 2
    decoded = np.frombuffer(base64.b64decode(data['audio']), dtype='<f4').reshape((-1, 2))
    np.testing.assert_allclose(decoded, [[1, .5], [.25, -.25]], atol=1e-7)

def test_bus_chain_and_diamond_route_accumulate_before_processing():
    mono = np.ones(8, np.float32)
    buses = [
        {"id": "a", "output": "b", "volume": 1, "effects": [], "sends": []},
        {"id": "b", "output": "master", "volume": 1, "effects": [], "sends": []},
        {"id": "c", "output": "master", "volume": 1, "effects": [], "sends": []},
    ]
    chain = mix_tracks([{"audio": mono, "bus": "a"}], 24000, {"master": {}, "buses": buses})
    expected = np.column_stack((mono / np.sqrt(2), mono / np.sqrt(2)))
    np.testing.assert_allclose(chain, expected, atol=1e-7)
    diamond = mix_tracks([{"audio": mono, "bus": "a", "sends": [{"to": "c", "level": .5}]}],
                         24000, {"master": {}, "buses": buses})
    np.testing.assert_allclose(diamond, expected * 1.5, atol=1e-7)


def test_bus_pan_applies_equal_power_stereo_balance():
    mono = np.ones(8, np.float32)
    out = mix_tracks([{"audio": mono, "bus": "panned"}], 24000,
                     {"master": {}, "buses": [{"id": "panned", "pan": 1}]})
    assert np.all(out[:, 0] == 0)
    assert np.allclose(out[:, 1], 1 / np.sqrt(2))
