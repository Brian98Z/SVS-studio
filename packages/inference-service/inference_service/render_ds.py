"""真 DiffSinger 渲染后端：调 abc2diffsinger 插件，整轨一次渲染（模型进程内复用）。

端点：POST /render_ds
  body: {notes: [{tick,duration,key,lyric}], tempos: [{tick,bpm}], singer: str, hash: str}
  返回: {ok, audio: b64(f32le 24000Hz), sample_rate, cache}

模型只在首次调用时加载（~20s），之后进程内复用（整轨一次推理，句间零开销）。
缓存：与 DSP 管线共用 AudioCache（整轨 hash）。
"""
from __future__ import annotations

import time
import sys
import os

from .config import ABC2VOCALS_DIR, VOICEBANK
TARGET_SR = 24000  # svs 工程播放采样率

_state = {"engine": None, "table": None}




def _load_engine(voicebank):
    """Load phoneme dictionary for the selected voice, not a global singer alias."""
    sys.path.insert(0, str(ABC2VOCALS_DIR))
    import abc2vocals as a2v
    key = str(voicebank)
    if _state.get('voicebank') != key:
        _state['engine'] = a2v
        _state['table'] = a2v.load_voicebank_dict(str(voicebank / 'dsdur' / 'dsdict-zh.yaml'))
        _state['voicebank'] = key
    return a2v


def _tempo_seconds(tempos, tick):
    from .contract import tick_to_ms

    return tick_to_ms(tempos or [{"tick": 0, "bpm": 120.0}], tick) / 1000.0


def _rebased_tempo_window(tempos, origin_tick):
    points = sorted(tempos or [{"tick": 0, "bpm": 120.0}], key=lambda p: p["tick"])

    active = points[0]
    for point in points:
        if point["tick"] <= origin_tick:
            active = point
        else:
            break
    rebased = [{"tick": 0, "bpm": active["bpm"]}]
    rebased.extend({"tick": point["tick"] - origin_tick, "bpm": point["bpm"]}
                   for point in points if point["tick"] > origin_tick)
    return rebased


def _resample_to(wav, sr_from, sr_to):
    import numpy as np
    if sr_from == sr_to:
        return wav.astype(np.float32)
    import librosa
    return librosa.resample(wav.astype(np.float32), orig_sr=sr_from, target_sr=sr_to)


def _segment_events(events, spq, max_seconds=18.0):
    """Return event groups with absolute score-relative seconds, excluding model padding."""
    segments = []
    current, start, elapsed, duration = [], 0.0, 0.0, 0.0
    for event in events:
        seconds = event["dur_q"] * spq
        if event['is_rest'] and seconds > max_seconds:
            if current:
                segments.append((current, start))
                current, duration = [], 0.0
            elapsed += seconds
            continue
        if seconds > max_seconds and not event["is_rest"]:
            raise ValueError("single sung note exceeds native synthesis window")
        if current and duration + seconds > max_seconds:
            segments.append((current, start))
            current, duration = [], 0.0
        if not current:
            start = elapsed
        current.append(event)
        duration += seconds
        elapsed += seconds
    if current:
        segments.append((current, start))
    return segments


def render_track(notes, tempos, hash_key: str, key_shift: int = 0, curves=None, offset_ticks: int = 0, singer='diffsinger.qixuan', voice=None):
    """Render native model conditioning, rejecting controls absent from the voicebank."""
    import yaml
    from .native_pipeline import decode_layers, validate_controls, render_sections, _voicebank_signature
    from pathlib import Path
    from .voices import resolve_voice
    import hashlib
    descriptor = resolve_voice(singer)
    if descriptor is None or descriptor.get('family') != 'DiffSinger' or not descriptor.get('path'):
        raise ValueError(f'No available DiffSinger voice directory registered for {singer}')
    if not descriptor.get('prerequisites', {}).get('diffsinger_topology_complete'):
        raise ValueError('DiffSinger model/vocoder prerequisites are incomplete or incompatible')
    voicebank = Path(descriptor['path'])
    vocoder_path = descriptor.get('vocoder_path')
    cache_key = hashlib.sha256(repr((hash_key, str(voicebank.resolve()), vocoder_path,
                                     _voicebank_signature(voicebank),
                                     _voicebank_signature(vocoder_path) if vocoder_path else ())).encode('utf-8')).hexdigest()
    decoded = decode_layers(curves)
    config = yaml.safe_load((voicebank / 'dsconfig.yaml').read_text(encoding='utf-8'))
    validate_controls(decoded, config)
    cache = _get_cache()
    cached = cache.get(hash_key, revision=cache_key)
    if cached is not None:
        return cached, True


    from .contract import tick_to_ms
    tempos = sorted(tempos or [{"tick": 0, "bpm": 120.0}], key=lambda p: p["tick"])
    origin_seconds = _tempo_seconds(tempos, offset_ticks)
    local_tempos = _rebased_tempo_window(tempos, offset_ticks)
    a2v = _load_engine(voicebank)
    import numpy as np
    bpm0 = float(local_tempos[0]["bpm"])
    spq = 60.0 / bpm0

    # 事件流：音符 + 空隙休止
    events = []
    prev_end = offset_ticks
    for n in sorted(notes, key=lambda x: x["tick"]):
        absolute_start = offset_ticks + n["tick"]
        absolute_end = absolute_start + n["duration"]
        if absolute_start < prev_end:
            raise ValueError("DiffSinger adapter does not support overlapping notes")
        gap = absolute_start - prev_end
        if gap > 4:
            gap_q = (tick_to_ms(tempos, absolute_start) - tick_to_ms(tempos, prev_end)) / 1000.0 / spq
            events.append({'midi': None, 'is_rest': True, 'dur_q': gap_q, 'lyric': None, '_note': None})
        dur_q = (tick_to_ms(tempos, absolute_end) - tick_to_ms(tempos, absolute_start)) / 1000.0 / spq
        breath = n.get('kind') == 'breath'
        events.append({'midi': n['key'] - key_shift, 'is_rest': False,
                       'dur_q': dur_q, 'lyric': n.get('lyric') if not breath and n.get('lyric') else '啊',
                       '_note': {**n, 'tick': absolute_start}})
        prev_end = absolute_end

    # ===== 内存防护：按 ~18s 分段（section）=====
    # render() 会按 section 逐段推理（模型只加载一次），段内存峰值 = 最长段的 mel 张量。
    # 整轨一次塞入会让 4 分钟曲子产生 16GB+ 中间张量（实测事故），必须切。
    lead_rest_sec = float(a2v.LEAD_REST_SEC)
    segments = _segment_events(events, spq)
    # Resolve polyphones in complete lyric context before per-syllable DS building.
    from pypinyin import pinyin, Style
    sung_events = [e for e in events if not e['is_rest'] and e.get('_note', {}).get('kind') != 'breath' and not e.get('_note', {}).get('phonemes')]
    context = ''.join(e['lyric'] for e in sung_events)
    pronunciations = pinyin(context, style=Style.NORMAL, errors='default')
    if len(pronunciations) == len(sung_events):
        for event, pronunciation in zip(sung_events, pronunciations):
            event['lyric'] = pronunciation[0]
    elif sung_events:
        raise ValueError('Native Mandarin notes require one syllable per note; use explicit phonemes for other languages')
    for event in events:
        if event.get('_note') and event['_note'].get('phonemes'):
            event['lyric'] = '啊'  # Builder placeholder is replaced with authoritative phonemes below.

    ds_sections = []
    for seg_events, segment_start in segments:
        parsed = {"bpm": bpm0, "beats": 4, "beat_type": 4, "key": 0, "events": seg_events,
                  "pitched_count": sum(1 for e in seg_events if not e["is_rest"]),
                  "missing_lyric": [], "lyric_tokens": 0, "lyric_mode": None, "voice": ""}
        secs, _meta = a2v.build_ds(parsed, _state["table"])
        for section in secs:
            section["offset"] = round(origin_seconds + segment_start, 4)
            counts = [int(v) for v in section['ph_num'].split()]
            phones = section['ph_seq'].split()
            cursor = counts[0]
            rebuilt = phones[:cursor]
            word_notes = [None]
            for index, event in enumerate(seg_events, 1):
                old_count = counts[index]
                note = event.get('_note')
                replacement = phones[cursor:cursor + old_count]
                if note and note.get('kind') == 'breath':
                    replacement = ['AP']
                elif note and note.get('phonemes'):
                    replacement = note['phonemes']
                rebuilt.extend(replacement)
                counts[index] = len(replacement)
                cursor += old_count
                word_notes.append(note)
            rebuilt.extend(phones[cursor:])
            word_notes.append(None)
            section['ph_seq'] = ' '.join(rebuilt)
            section['ph_num'] = ' '.join(map(str, counts))
            section['_svs_word_notes'] = word_notes
            section['_svs_notes'] = [e['_note'] for e in seg_events if e.get('_note')]
        ds_sections.extend(secs)

    t0 = time.perf_counter()
    preference = os.environ.get("SVS_INFERENCE_DEVICE")
    if preference is None:
        # Keep the legacy explicit CUDA opt-in; otherwise auto proves a usable provider.
        preference = "cuda" if os.environ.get("SVS_INFERENCE_GPU") == "1" else "auto"
    wav, sr_out = render_sections(a2v, ds_sections, voicebank, decoded, tempos, lead_rest_sec, preference,
                                 voice or {}, vocoder_path=vocoder_path)
    elapsed = time.perf_counter() - t0
    pcm = _resample_to(wav, sr_out, TARGET_SR)
    pcm = pcm[round(lead_rest_sec * TARGET_SR):]
    # Fixed gain, not per-render peak normalization: model energy edits must survive.
    pcm = (pcm * 2.0).astype(np.float32)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak > 0.99:
        pcm *= 0.99 / peak
    print(f"[render_ds] 分段渲染 {len(ds_sections)} 段 耗时 {elapsed:.1f}s → {len(pcm)/TARGET_SR:.1f}s 音频", flush=True)

    cache.put(hash_key, pcm, revision=cache_key)
    return pcm, False


def _get_cache():
    # A -m launch names the running service __main__; importing it again would
    # create a second cache invisible to /invalidate and /health.
    running = sys.modules.get('__main__')
    if getattr(getattr(running, '__spec__', None), 'name', None) == 'inference_service.service_runner':
        return running.cache
    from .service_runner import cache
    return cache
