"""Unit tests for copy-on-write native DiffSinger speaker selection and embedding blends."""
import numpy as np
from types import SimpleNamespace
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
        for stage, attribute, component in (
            ("pred_duration", "dsdur", "dsdur"),
            ("pred_pitch", "dspitch", "dspitch"),
            ("pred_variance", "dsvariance", "dsvariance"),
            ("pred_acoustic", "ds_acoustic", "dsacoustic"),
        ):
            reader = getattr(self, component)
            consumer = SimpleNamespace(**{attribute: reader})
            if component != "dsacoustic":
                consumer.input_processor = SimpleNamespace(vb_reader=reader)
            setattr(self, stage, consumer)


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


def test_real_acoustic_inputs_consume_blend_without_changing_cached_speaker():
    acoustic = pytest.importorskip("diffsinger_utau.voice_bank.pred_acoustic")
    pred = Predictor()
    reader = pred.dsacoustic
    reader.phonemes = SimpleNamespace(content={"a": 1})
    reader.languages = SimpleNamespace(content={})
    reader.use_lang_id = False
    reader.acoustic_model = SimpleNamespace(session=True, input_names=["tokens", "durations", "f0", "spk_embed"])
    pred.pred_acoustic = object.__new__(acoustic.PredAcoustic)
    pred.pred_acoustic.ds_acoustic = reader
    pred.pred_acoustic.timestep = 512 / 44100
    section = acoustic.DSReader.DSSection({
        "offset": 0, "text": "a", "ph_seq": "a", "ph_num": "1",
        "ph_dur": "0.1", "note_seq": "A4", "note_slur": "0", "note_dur": "0.1",
        "f0_seq": "440 440 440 440 440 440 440 440 440", "f0_timestep": str(512 / 44100),
    })
    clone, alias = prepare_speaker(pred, {"blend": [
        {"speaker_id": "alto", "weight": .25}, {"speaker_id": "tenor", "weight": .75},
    ]})
    inputs = clone.pred_acoustic._prepare_inputs(section, lang="zh", speaker=alias)
    np.testing.assert_allclose(inputs["spk_embed"], np.broadcast_to([.25, .75], inputs["spk_embed"].shape))
    original = pred.pred_acoustic._prepare_inputs(section, lang="zh", speaker="alto")
    np.testing.assert_allclose(original["spk_embed"], np.broadcast_to([1., 0.], original["spk_embed"].shape))
