"""ABC 曲谱导入桥：复用 abc2diffsinger 插件的解析管线。

stdin  收 JSON: {abc: str, voice?: str, lyrics?: str, key_shift?: int}
stdout 出 JSON: {ok, bpm, beats, beat_type, key, voice, voices: [...],
                 notes: [{tick, duration, key(midi), velocity, lyric}], missing_lyric}

歌词处理：
  - lyrics 非空 → 注入选中声部 w: 行（逐字切分，与插件 split_lyrics 一致）
  - lyrics 为空 → 无词音符补 "a"（啊啊啊试唱，插件同款占位）
依赖：music21 在本机 Python 3.11（插件 README 已验证）。abc2vocals 以源码方式导入。
"""
from __future__ import annotations

import io
import os
import json
import tempfile
import sys
from pathlib import Path

# 插件目录：环境变量 SVS_ABC2VOCALS_DIR 可覆盖（与 inference_service/config.py 同名变量一致）
ABC2VOCALS_DIR = Path(os.environ.get(
    "SVS_ABC2VOCALS_DIR",
    r"F:/OMP/projects/项目与插件/abc2diffsinger",
))
PPQ = 480  # 与插件 TICKS_PER_QUARTER 一致，也是 svs 工程 PPQ


def main() -> int:
    req = json.loads(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8").read())
    return main_with(req)


def main_with(req: dict) -> int:
    abc_text: str = req["abc"]
    voice: str | None = req.get("voice") or None
    lyrics: str = (req.get("lyrics") or "").strip()
    key_shift: int = int(req.get("key_shift") or 0)

    sys.path.insert(0, str(ABC2VOCALS_DIR))
    import abc2vocals as a2v  # noqa: E402

    # 歌词注入：写临时 .abc（inject_lyrics 以行文本工作，parse_abc 需要文件路径）
    # 无 V: 行的谱面默认声部名是空串，inject_lyrics 按 V: 上下文匹配必须传 '' 而非 None
    tmp = Path(tempfile.gettempdir()) / "svs_abc_import_tmp.abc"
    work_text = abc_text
    if lyrics:
        work_text = a2v.inject_lyrics(abc_text, voice or "", a2v.split_lyrics(lyrics))
    tmp.write_text(work_text, encoding="utf-8")

    try:
        parsed = a2v.parse_abc(str(tmp), voice=voice)
    finally:
        tmp.unlink(missing_ok=True)

    events = parsed["events"]
    # 事件偏移（四分音符）→ tick；休止产生空隙（不生成音符）
    tick_acc = 0
    notes = []
    for ev in events:
        dur_ticks = round(ev["dur_q"] * PPQ)
        if not ev["is_rest"] and ev.get("midi") is not None:
            midi = ev["midi"] + key_shift
            midi = max(0, min(127, midi))
            lyric = ev.get("lyric") or "a"
            notes.append({
                "tick": tick_acc,
                "duration": max(dur_ticks, 30),  # 极短音符保底（装饰音）
                "key": midi,
                "velocity": 1.0,
                "lyric": lyric,
                "legato": False,
            })
        tick_acc += dur_ticks

    # 和弦标记提取：选中声部音乐行内引号段（如 "G#m"），按 | 切小节计位置
    import re as _re
    chords_out = []
    cur_voice_ctx = None
    bar_idx = 0
    for raw in abc_text.splitlines():
        s = raw.strip()
        vm = _re.match(r"^V\s*:\s*(\S+)", s)
        if vm:
            cur_voice_ctx = vm.group(1)
            continue
        if _re.match(r"^[A-Za-z]\s*:", s) or not s or s.startswith("%"):
            continue
        target_voice = voice or parsed.get("voice", "")
        has_voice_decl = bool(_re.search(r"^V\s*:", abc_text, _re.M))
        if has_voice_decl and cur_voice_ctx != target_voice:
            continue  # 其它声部行不计小节（各声部小节流独立）
        segs = s.split("|")
        if segs and segs[-1].strip() == "":
            segs.pop()  # 行尾竖线产生的空段不是小节
        for seg in segs:
            marks = _re.findall(r'"([^"]+)"', seg)
            if marks:
                chords_out.append({"bar": bar_idx, "name": marks[0]})
            bar_idx += 1
    beats_per_bar = parsed.get("beats") or 4
    bar_ticks = beats_per_bar * PPQ
    chords_out = [{"tick": c["bar"] * bar_ticks, "name": c["name"]} for c in chords_out]

    out = {
        "ok": True,
        "bpm": parsed["bpm"],
        "beats": parsed["beats"] or 4,
        "beat_type": parsed["beat_type"] or 4,
        "key": parsed.get("key", 0),
        "voice": parsed.get("voice", ""),
        "voices": a2v.split_abc_voices(abc_text)["order"],
        "notes": notes,
        "total_ticks": tick_acc,
        "missing_lyric": len(parsed.get("missing_lyric", [])),
        "lyric_mode": parsed.get("lyric_mode"),
        "chords": chords_out,
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


def ai_lyrics(req: dict) -> int:
    """AI 按音符数生成歌词：{theme, n_notes, phrase_lens?, abc?, voice?}。

    phrase_lens（每句音节数）由 abc 的休止结构算出（a2v.phrase_lengths），
    让模型按乐句分行。key 从插件保存的 config/llm.json 或环境变量取。
    """
    theme = (req.get("theme") or "").strip()
    n_notes = int(req.get("n_notes") or 0)
    if not theme:
        json.dump({"ok": False, "error": "主题为空"}, sys.stdout, ensure_ascii=False)
        return 1
    sys.path.insert(0, str(ABC2VOCALS_DIR))
    import abc2vocals as a2v  # noqa: E402

    phrase_lens = None
    abc_text = req.get("abc")
    if abc_text and req.get("voice") is not None:
        tmp = Path(tempfile.gettempdir()) / "svs_abc_lyr_tmp.abc"
        tmp.write_text(abc_text, encoding="utf-8")
        try:
            parsed = a2v.parse_abc(str(tmp), voice=req.get("voice") or None)
            lens = a2v.phrase_lengths(parsed)
            if lens and sum(lens) == n_notes:
                phrase_lens = lens
        except Exception:  # noqa: BLE001 — 谱面解析失败不阻塞填词
            pass
        finally:
            tmp.unlink(missing_ok=True)

    cfg = a2v.load_saved_llm_config()
    text, exact = a2v.ai_write_lyrics(
        theme, n_notes,
        base_url=cfg.get("url") or a2v.DEFAULT_LLM_BASE_URL,
        api_key=cfg.get("key"),
        model=cfg.get("model") or a2v.DEFAULT_LLM_MODEL,
        phrase_lens=phrase_lens,
    )
    json.dump({"ok": True, "lyrics": text, "exact": exact, "n_notes": n_notes,
               "phrase_lens": phrase_lens},
              sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    # 双模式：默认 ABC 导入；{"mode":"ai_lyrics"} 走 AI 填词
    _raw = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8").read()
    _req = json.loads(_raw)
    if _req.get("mode") == "ai_lyrics":
        sys.exit(ai_lyrics(_req))
    # 原 main() 重新读 stdin 已不可能——把解析好的请求传进去
    sys.exit(main_with(_req))
