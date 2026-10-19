import math

import pytest

from inference_service.expression import breath_ds_event, retime_consonant, vibrato_curve


def test_vibrato_uses_absolute_ticks_tempo_map_and_note_local_ramps():
    notes = [{"tick": 480, "duration": 960, "vibrato": {
        "depth_cents": 100, "frequency_hz": 2, "delay_ms": 0,
        "onset_ms": 250, "release_ms": 250,
    }}]
    tempos = [{"tick": 0, "bpm": 120}, {"tick": 960, "bpm": 60}]
    curve = vibrato_curve(notes, tempos, 0, 1680)
    assert all(math.isnan(x) for x in curve[:480])
    assert curve[480] == pytest.approx(0.0)
    assert curve[720] == pytest.approx(0.0)  # half-cycle at 2 Hz
    assert curve[600] == pytest.approx(50.0)  # onset ramp reaches half depth after 125 ms
    assert all(math.isnan(x) for x in curve[1440:])


def test_vibrato_adds_to_finite_user_edits_but_keeps_sparse_holes():
    notes = [{"tick": 0, "duration": 480, "vibrato": {
        "depth_cents": 50, "frequency_hz": 5, "delay_ms": 0,
    }}]
    existing = [12.0] + [math.nan] * 479 + [-7.0]
    result = vibrato_curve(notes, [{"tick": 0, "bpm": 120}], 0, 481, existing)
    assert result[0] == pytest.approx(12.0)
    assert math.isfinite(result[120])
    assert result[480] == -7.0


def test_vibrato_note_selection_is_explicit_and_untouched_notes_stay_sparse():
    selected = {"tick": 120, "duration": 240, "vibrato": {"depth_cents": 20, "frequency_hz": 4, "delay_ms": 0}}
    unselected = {"tick": 480, "duration": 240, "vibrato": {"depth_cents": 90, "frequency_hz": 7, "delay_ms": 0}}
    result = vibrato_curve([selected], [{"tick": 0, "bpm": 120}], 0, 720)
    assert all(math.isnan(x) for x in result[:120])
    assert all(math.isnan(x) for x in result[360:])
    assert all(math.isnan(x) for x in vibrato_curve([], [{"tick": 0, "bpm": 120}], 0, 720))


def test_breath_event_uses_real_ap_phoneme_not_noise_or_rest():
    assert breath_ds_event(0.5) == {"midi": None, "is_rest": False, "dur_q": 0.5, "lyric": "AP"}
    with pytest.raises(ValueError):
        breath_ds_event(0)


@pytest.mark.parametrize("placement,expected", [
("head", [50.0, 15.0, 15.0]),
("tail", [8.0, 12.0, 60.0]),
])
def test_consonant_retiming_conserves_duration_and_selects_edge(placement, expected):
    original = [20.0, 30.0, 30.0]
    result = retime_consonant(original, 30.0, placement)
    assert result == pytest.approx(expected)
    assert sum(result) == pytest.approx(sum(original))
    assert min(result) >= 0


def test_consonant_retiming_clamps_at_available_duration():
    result = retime_consonant([8.0, 2.0], 20.0, "head")
    assert result == pytest.approx([10.0, 0.0])
    result = retime_consonant([8.0, 2.0], -20.0, "head")
    assert result == pytest.approx([0.0, 10.0])
    with pytest.raises(ValueError):
        retime_consonant([1.0], 3.0, "middle")
