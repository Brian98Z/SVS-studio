"""Score-level vocal expression helpers; no waveform synthesis is performed here."""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .contract import tick_to_ms


def vibrato_curve(
    notes: Sequence[Mapping[str, Any]],
    tempos: Sequence[Mapping[str, Any]],
    start_tick: int,
    end_tick: int,
    user_curve: Sequence[float] | None = None,
) -> list[float]:
    """Return additive cents at each absolute tick in ``[start_tick, end_tick)``.

    NaNs in the existing user layer are treated as an unedited zero baseline;
    the returned layer remains NaN outside selected vibrato notes.  Existing
    finite user edits are added to vibrato, so callers can store this as the
    resulting user layer without erasing hand-drawn pitch.
    """
    if start_tick < 0 or end_tick < start_tick:
        raise ValueError("tick range must be nonnegative and ordered")
    count = end_tick - start_tick
    if user_curve is not None and len(user_curve) != count:
        raise ValueError("user_curve length must match tick range")
    result = [math.nan if user_curve is None or not math.isfinite(float(value)) else float(value)
              for value in (user_curve if user_curve is not None else [math.nan] * count)]
    for note in notes:
        vibrato = note.get("vibrato")
        if not vibrato:
            continue
        depth = float(vibrato.get("depth_cents", 0.0))
        frequency = float(vibrato.get("frequency_hz", 0.0))
        delay = float(vibrato.get("delay_ms", 0.0))
        onset = max(0.0, float(vibrato.get("onset_ms", 0.0)))
        release = max(0.0, float(vibrato.get("release_ms", 0.0)))
        if not all(math.isfinite(x) for x in (depth, frequency, delay, onset, release)):
            raise ValueError("vibrato values must be finite")
        if depth < 0 or frequency <= 0 or delay < 0 or min(onset, release) < 0:
            raise ValueError("vibrato depth, frequency, and timing are out of range")
        note_start = int(note["tick"])
        note_end = note_start + int(note["duration"])
        note_start_ms = tick_to_ms(tempos, note_start)
        note_end_ms = tick_to_ms(tempos, note_end)
        active_ms = note_start_ms + delay
        for tick in range(max(start_tick, note_start), min(end_tick, note_end)):
            idx = tick - start_tick
            t_ms = tick_to_ms(tempos, tick)
            elapsed = t_ms - active_ms
            if elapsed < 0:
                continue
            envelope = min(1.0, elapsed / onset) if onset else 1.0
            if release:
                envelope *= min(1.0, max(0.0, (note_end_ms - t_ms) / release))
            vib = depth * envelope * math.sin(2.0 * math.pi * frequency * elapsed / 1000.0)
            baseline = 0.0 if not math.isfinite(result[idx]) else result[idx]
            result[idx] = baseline + vib
    return result


def breath_ds_event(duration_quarters: float) -> dict[str, Any]:
    """Build a DiffSinger AP (aspiration) phoneme event for an actual breath.

    ``AP`` is a phoneme token, not synthetic noise; synthesis is delegated to
    the voicebank's trained phoneme model. Duration is in quarter notes.
    """
    duration = float(duration_quarters)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("breath duration must be finite and positive")
    return {"midi": None, "is_rest": False, "dur_q": duration, "lyric": "AP"}


def retime_consonant(
    phoneme_durations_ms: Sequence[float],
    amount_ms: float,
    placement: str,
) -> list[float]:
    """Move duration to/from the first (head) or last (tail) phoneme.

    Positive amounts lengthen the selected edge phoneme, borrowing time
    proportionally from all other phonemes. Negative amounts shorten it and
    distribute the released time proportionally to the remaining phonemes.
    The total is conserved and no duration becomes negative.
    """
    durations = [float(value) for value in phoneme_durations_ms]
    amount = float(amount_ms)
    if not durations or any(not math.isfinite(x) or x < 0 for x in durations):
        raise ValueError("phoneme durations must be a nonempty nonnegative finite sequence")
    if not math.isfinite(amount) or placement not in ("head", "tail"):
        raise ValueError("amount must be finite and placement must be head or tail")
    edge = 0 if placement == "head" else len(durations) - 1
    others = [i for i in range(len(durations)) if i != edge]
    if not others:
        return durations
    if amount > 0:
        available = sum(durations[i] for i in others)
        moved = min(amount, available)
        if available:
            for i in others:
                durations[i] -= moved * durations[i] / available
        durations[edge] += moved
    elif amount < 0:
        moved = min(-amount, durations[edge])
        available = sum(durations[i] for i in others)
        durations[edge] -= moved
        if available:
            for i in others:
                durations[i] += moved * durations[i] / available
        else:
            durations[edge] += moved
    return durations
