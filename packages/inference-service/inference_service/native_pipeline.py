"""Stage-level DiffSinger inference; edits condition models before vocoding.

Pitch is a cents offset from predicted F0. Air is breathiness in the model's
log-amplitude domain, normalized -96..0 dB to 0..1. Gender is key-shift
conditioning (-1..1 mapped from 0..1); velocity is time-stretch conditioning.
Unsupported model inputs are rejected, never replaced by waveform effects.
"""
from __future__ import annotations

import base64
import math
import threading
from pathlib import Path
import numpy as np

_PREDICTOR_LOCK = threading.RLock()
_PREDICTOR_CACHE: tuple[tuple, object, object] | None = None


def _voicebank_signature(path):
    """Invalidate the single heavyweight predictor if any ONNX/config asset changes."""
    root = Path(path).resolve()
    files = sorted((p for p in root.rglob("*") if p.is_file() and
                    (p.suffix.lower() == ".onnx" or p.name in {"dsconfig.yaml", "vocoder.yaml"})),
                   key=lambda item: str(item).casefold())
    return tuple((str(item), item.stat().st_size, item.stat().st_mtime_ns) for item in files)

LAYERS = ('base', 'user', 'envelope', 'direct', 'global')


def decode_layers(curves):
    result = {}
    for param, layers in (curves or {}).items():
        if isinstance(layers, str):
            layers = {'user': layers}
        decoded = {}
        for name in LAYERS:
            raw = base64.b64decode(layers.get(name, ''), validate=True)
            if len(raw) % 4:
                raise ValueError(f'{param}/{name}: unaligned f32 curve')
            decoded[name] = np.frombuffer(raw, dtype='<f4')
        if any(np.any(np.isfinite(a)) for a in decoded.values()):
            result[param] = decoded
    return result


def sample_layers(layers, ticks, baseline, param):
    """Sparse holes preserve the model prediction; Direct replaces lower layers."""
    value = np.broadcast_to(np.asarray(baseline, dtype=np.float32), ticks.shape).copy()
    for name in LAYERS:
        a = layers[name]
        valid = (ticks >= 0) & (ticks < len(a))
        samples = np.full(ticks.shape, np.nan, dtype=np.float32)
        samples[valid] = a[ticks[valid]]
        present = np.isfinite(samples)
        if name in ('base', 'direct'):
            value[present] = samples[present]
        else:
            value[present] += samples[present]
    return np.clip(value, -1200 if param == 'pitch' else 0, 1200 if param == 'pitch' else 1)


def frame_ticks(count, timestep, section_seconds, lead_seconds, tempos):
    """Absolute score ticks, retaining negative model-padding positions."""
    from .contract import ms_to_tick
    seconds = section_seconds - lead_seconds + np.arange(count) * timestep
    return np.asarray([ms_to_tick(tempos, s * 1000) if s >= 0 else -1 for s in seconds], dtype=np.int64)


def validate_controls(decoded, config):
    supported = {'pitch'}
    for param, flag in [('air', 'use_breathiness_embed'), ('energy', 'use_energy_embed'),
                        ('tension', 'use_tension_embed'), ('gender', 'use_key_shift_embed'),
                        ('velocity', 'use_speed_embed')]:
        if config.get(flag):
            supported.add(param)
    unsupported = sorted(set(decoded) - supported)
    if unsupported:
        raise ValueError('Voicebank has no native conditioning for: ' + ', '.join(unsupported))
    return supported


def predict_section(pred, section, decoded, tempos, lead_seconds, speaker=None):
    from diffsinger_utau.voice_bank.commons.ds_reader import DSReader
    from diffsinger_utau.voice_bank.commons.utils import resample_align_curve
    ds = DSReader.DSSection(dict(section))
    if speaker is None and pred.available_speakers:
        speaker = pred.available_speakers[0]
    if speaker is not None and speaker not in pred.available_speakers:
        raise ValueError(f'Unknown native speaker: {speaker}')
    durations = pred.pred_duration.predict(ds, lang='zh', speaker=speaker)
    notes = section.get('_svs_notes', [])
    if section.get('_svs_word_notes') and len(durations):
        from .expression import retime_consonant
        durations = np.asarray(durations, dtype=np.float32).copy()
        cursor = 0
        counts = [int(v) for v in ds['ph_num'].split()]
        for count, note in zip(counts, section['_svs_word_notes']):
            if note is not None and count > 1:
                part = durations[cursor:cursor + count]
                if note.get('attack_ms') is not None:
                    part = np.asarray(retime_consonant(part * 1000., float(note['attack_ms']), 'head'), dtype=np.float32) / 1000.
                if note.get('decay_ms') is not None:
                    part = np.asarray(retime_consonant(part * 1000., float(note['decay_ms']), 'tail'), dtype=np.float32) / 1000.
                durations[cursor:cursor + count] = part
            cursor += count
    ds['ph_dur'] = ' '.join(map(str, durations.tolist()))
    f0 = np.asarray(pred.pred_pitch.predict(ds, lang='zh', speaker=speaker, key_shift=0, steps=10), dtype=np.float32)
    ticks = frame_ticks(len(f0), pred.pred_pitch.timestep, float(ds['offset']), lead_seconds, tempos)
    if notes:
        from .expression import vibrato_curve
        first = int(section.get('_svs_curve_start_tick', max(0, int(ticks[ticks >= 0][0]) if np.any(ticks >= 0) else 0)))
        end = first + max(0, int(ticks.max(initial=-1)) - first + 1)
        curve = np.asarray(vibrato_curve(notes, tempos, first, end), dtype=np.float32)
        valid = (ticks >= first) & (ticks < end)
        cents = np.zeros(ticks.shape, dtype=np.float32)
        cents[valid] = np.nan_to_num(curve[ticks[valid] - first], nan=0.0)
        f0 *= np.exp2(cents / 1200.0)
    if 'pitch' in decoded:
        cents = sample_layers(decoded['pitch'], ticks, 0.0, 'pitch')
        f0 = f0 * np.exp2(cents / 1200.0)

    ds['f0_seq'] = ' '.join(map(str, f0.tolist()))
    ds['f0_timestep'] = str(pred.pred_pitch.timestep)
    variance = pred.pred_variance.predict(ds, lang='zh', speaker=speaker, key_shift=0, steps=10, retake_all=True)
    for name, values in variance.items():
        param = {'breathiness': 'air'}.get(name, name)
        values = np.asarray(values, dtype=np.float32)
        if param in decoded:
            ticks = frame_ticks(len(values), pred.pred_variance.timestep, float(ds['offset']), lead_seconds, tempos)
            if name in ('breathiness', 'energy'):
                baseline = (values + 96.0) / 96.0
                values = sample_layers(decoded[param], ticks, baseline, param) * 96.0 - 96.0
            else:
                values = sample_layers(decoded[param], ticks, values, param)
        ds[name] = ' '.join(map(str, values.tolist()))
        ds[f'{name}_timestep'] = str(pred.pred_variance.timestep)
    inputs = pred.pred_acoustic._prepare_inputs(ds, lang='zh', speaker=speaker, gender=0)
    length = inputs['f0'].shape[1]
    timestep = pred.pred_acoustic.timestep
    ticks = frame_ticks(length, timestep, float(ds['offset']), lead_seconds, tempos)
    if 'gender' in decoded:
        values = sample_layers(decoded['gender'], ticks, .5, 'gender') * 2.0 - 1.0
        lo, hi = pred.dsacoustic.augmentation_args.get('random_pitch_shifting', {}).get('range', [-5., 5.])
        inputs['gender'] = (values * np.where(values >= 0, hi, abs(lo)))[None].astype(np.float32)
    if 'velocity' in decoded:
        values = sample_layers(decoded['velocity'], ticks, .5, 'velocity')
        lo, hi = pred.dsacoustic.augmentation_args.get('random_time_stretching', {}).get('range', [.8, 1.2])
        inputs['velocity'] = (lo + values * (hi - lo))[None].astype(np.float32)
    inputs['steps'] = np.asarray(20, dtype=np.int64)
    mel = pred.dsacoustic.acoustic_model.predict(inputs)
    vf0 = resample_align_curve(f0, pred.pred_pitch.timestep, pred.pred_vocoder.timestep, mel.shape[1])
    wav = pred.pred_vocoder.predict(mel, vf0)
    return np.asarray(wav, dtype=np.float32).reshape(-1)


def render_sections(a2v, sections, voicebank, decoded, tempos, lead_seconds, device, voice=None, vocoder_path=None):
    """Render with one bounded predictor, serialized across HTTP worker threads."""
    global _PREDICTOR_CACHE
    from .hardware import setup_runtime, validate_preference
    from .voice_runtime import prepare_speaker

    preference = validate_preference(device)
    signature = (str(Path(voicebank).resolve()), str(Path(vocoder_path).resolve()) if vocoder_path else None,
                 preference, _voicebank_signature(voicebank), _voicebank_signature(vocoder_path) if vocoder_path else ())
    with _PREDICTOR_LOCK:
        if _PREDICTOR_CACHE is None or _PREDICTOR_CACHE[0] != signature:
            from .vocoder_binding import build_predictor
            pred = build_predictor(Path(voicebank), vocoder_path)
            report = setup_runtime(pred, preference=preference, a2v=a2v)
            _PREDICTOR_CACHE = (signature, pred, report)
        _, pred, report = _PREDICTOR_CACHE
        # prepare_speaker returns a copy-on-write clone for blends; never mutate cached readers.
        render_pred, speaker = prepare_speaker(pred, voice or {})
        waves = [(float(section['offset']), predict_section(render_pred, section, decoded, tempos, lead_seconds, speaker))
                 for section in sections]
        sr = render_pred.dsvocoder.sample_rate
        origin = min((off for off, _ in waves), default=0.)
        total = max((round((off - origin) * sr) + len(wav) for off, wav in waves), default=0)
        mixed = np.zeros(total, dtype=np.float32)
        for off, wav in waves:
            start = round((off - origin) * sr)
            mixed[start:start + len(wav)] += wav
        from .hardware import publish_runtime
        publish_runtime(report)
        return mixed, sr
