"""Unit tests for copy-on-write native DiffSinger speaker selection and embedding blends."""
import numpy as np
import pytest

from inference_service.voice_runtime import prepare_speaker


class Speaker:
    def __init__(self, name, values):
        self.speaker_name = name
        self.values = np.asarray(values, dtype=np.float32)

    def get_embed(self):
        return self.values


class Component:
    def __init__(self):
        self.speakers = [Speaker("alto", [1, 0]), Speaker("tenor", [0, 1])]


class Predictor:
    def __init__(self):
        for name in ("dsdur", "dspitch", "dsvariance", "dsacoustic"):
            setattr(self, name, Component())
        self.available_speakers = ["alto", "tenor"]


def test_selection_requires_all_component_support():
    pred = Predictor()
    same, name = prepare_speaker(pred, {"speaker_id": "tenor"})
    assert same is pred
    assert name == "tenor"
    pred.dsvariance.speakers = [Speaker("alto", [1, 0])]
    with pytest.raises(ValueError, match="Unknown/incompatible"):
        prepare_speaker(pred, {"speaker_id": "tenor"})


def test_blend_is_copy_on_write_and_shared_alias_everywhere():
    pred = Predictor()
    clone, alias = prepare_speaker(pred, {"blend": [
        {"speaker_id": "alto", "weight": 0.25},
        {"speaker_id": "tenor", "weight": 0.75},
    ]})
    assert clone is not pred
    assert alias.startswith("svs-blend-")
    assert alias in clone.available_speakers
    for component_name in ("dsdur", "dspitch", "dsvariance", "dsacoustic"):
        original = getattr(pred, component_name)
        cloned = getattr(clone, component_name)
        assert cloned is not original
        assert [s.speaker_name for s in original.speakers] == ["alto", "tenor"]
        blend = next(s for s in cloned.speakers if s.speaker_name == alias)
        np.testing.assert_allclose(blend.get_embed(), [0.25, 0.75])


def test_invalid_blend_and_emotion_are_rejected():
    pred = Predictor()
    for setting in (
        {"blend": [{"speaker_id": "alto", "weight": float("nan")}]},
        {"blend": [{"speaker_id": "alto", "weight": 0.5}]},
        {"emotion": "happy"},
    ):
        with pytest.raises(ValueError):
            prepare_speaker(pred, setting)
