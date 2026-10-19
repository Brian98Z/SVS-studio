"""《星海之梦》— 2 分钟完整测试曲目构建脚本。

结构（120BPM 4/4，每小节 1920 tick = 2s，共 60 小节 = 120s）：
  前奏 1-4 | 主歌A 5-12 | 预副歌 13-16 | 副歌 17-28 | 间奏 29-32
  | 主歌B 33-40 | 副歌2 41-52（和声）| 尾奏 53-60

写入当前 server 工程（POST /api/*），含：
  - 主唱 sing 轨（歌词+音素走 server g2p_inline）
  - 和声 sing 轨（副歌2 段三度平行）
  - 钢琴 instrument 轨（和弦分解伴奏）
  - Chord 轨（和弦标记）
  - 8 通道表现力曲线（pitch 颤音/滑音、energy 段落强弱、air/tension 音色变化）
"""
import json
import urllib.request

from midi_import import loop_to_notes

BASE = "http://127.0.0.1:8761/api"
PPQ = 480
BAR = 1920  # 4/4 每小节 tick


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    r = urllib.request.urlopen(req, timeout=120)
    text = r.read().decode()
    return json.loads(text) if text.strip() else {}


# ---------------- 乐理素材 ----------------
# 和弦进行（C 大调流行：1-5-6m-4h 变体）
PROG_VERSE = [("C", [60, 64, 67]), ("G", [55, 59, 62]), ("Am", [57, 60, 64]), ("F", [53, 57, 60])]
PROG_CHORUS = [("F", [53, 57, 60]), ("G", [55, 59, 62]), ("Em", [52, 55, 59]), ("Am", [57, 60, 64])]
PROG_CHORUS2 = [("F", [53, 57, 60]), ("G", [55, 59, 62]), ("C", [60, 64, 67]), ("C", [60, 64, 67])]

# 歌词（每音节一个字；空格分隔音符）
LYRICS_VERSE_A = "夜 空 中 星 光 在 闪 烁 | 梦 的 船 正 在 启 航 | 风 吹 过 指 尖 的 温 柔 | 带 我 去 遥 远 地 方"
LYRICS_VERSE_B = "时 光 在 心 里 面 流 淌 | 记 忆 的 门 慢 慢 打 开 | 那 些 未 说 出 的 期 待 | 都 化 作 星 海 的 光"
LYRICS_CHORUS = "飞 吧 飞 向 那 星 海 | 让 梦 想 尽 情 盛 开 | 无 论 路 有 多 遥 远 | 心 中 有 光 就 不 黑"
LYRICS_OUTRO = "星 海 之 梦 永 不 散"


def parse_lyrics(block):
    """'字 字 字 | 字 字' → 按小节分组的字列表"""
    return [bar.split() for bar in block.split("|")]


def melody_notes(bar_start_tick, syllables, scale, rhythm=None):
    """小节内音符：默认节奏 4 分音符x4；scale 为可用音高级数列表（循环取音）。
    返回 [(tick, dur, key, lyric, legato)]"""
    out = []
    t = bar_start_tick
    rhythm = rhythm or [BAR // max(len(syllables), 1)] * len(syllables)  # 字数均分小节
    for i, syl in enumerate(syllables):
        dur = rhythm[i % len(rhythm)]
        key = scale[i % len(scale)]
        out.append((t, dur, key, syl, False))
        t += dur
    return out


# 旋律音阶（度数 → MIDI key）
VERSE_SCALE_A = [60, 62, 64, 65, 67, 65, 64, 62]      # C4-E4 区
VERSE_SCALE_B = [62, 64, 65, 67, 69, 67, 65, 64]      # D4-A4
PRECHORUS_SCALE = [64, 65, 67, 69, 71, 69, 67, 65]    # E4-B4 上行
CHORUS_SCALE = [67, 69, 72, 71, 69, 67, 65, 64]       # G4-C5 区
OUTRO_SCALE = [72, 71, 69, 67]                        # 长音收束


def build():
    # ---------- 新工程 ----------
    api("POST", "/project/new")

    # ---------- 轨道 ----------
    lead = api("POST", "/track", {"kind": "sing", "name": "主唱"})["id"]
    harm = api("POST", "/track", {"kind": "sing", "name": "和声"})["id"]
    piano = api("POST", "/track", {"kind": "instrument", "name": "钢琴"})["id"]
    drum_tr = api("POST", "/track", {"kind": "instrument", "name": "鼓机"})["id"]
    bass_tr = api("POST", "/track", {"kind": "instrument", "name": "贝斯"})["id"]
    chord_tr = api("POST", "/track", {"kind": "chord", "name": "和弦"})["id"]
    api("PATCH", f"/track/{harm}", {"volume": 0.5, "singer_id": "builtin.fallback"})
    api("PATCH", f"/track/{piano}", {"volume": 0.45, "instrument": "builtin.piano"})
    api("PATCH", f"/track/{drum_tr}", {"volume": 0.9, "instrument": "builtin.piano"})
    api("PATCH", f"/track/{bass_tr}", {"volume": 0.75, "instrument": "builtin.piano"})
    # SoundBug 式轨道配色
    for tid, colour in [(lead, "#5b8def"), (harm, "#8b7bd8"), (piano, "#4a9d8f"),
                        (drum_tr, "#e0a45c"), (bass_tr, "#c9564a")]:
        api("PATCH", f"/track/{tid}", {"colour": colour})

    # ---------- 和弦轨 + 钢琴伴奏（全曲） ----------
    def prog_at(bar):
        if bar < 4:
            return PROG_VERSE
        if bar < 12:
            return PROG_VERSE
        if bar < 16:
            return PROG_CHORUS[:2] * 2
        if bar < 28:
            return PROG_CHORUS
        if bar < 32:
            return PROG_VERSE
        if bar < 40:
            return PROG_VERSE
        if bar < 52:
            return PROG_CHORUS2
        return PROG_VERSE

    for bar in range(60):
        chord_name, triad = prog_at(bar)[bar % 4]
        t0 = bar * BAR
        # 和弦轨标记（整小节）
        api("POST", f"/track/{chord_tr}/note",
            {"tick": t0, "duration": BAR, "key": triad[0], "lyric": chord_name})
        # 钢琴：根音全音符 + 分解琶音（8 分音符）
        api("POST", f"/track/{piano}/note",
            {"tick": t0, "duration": BAR, "key": triad[0] - 12, "velocity": 0.55, "lyric": ""})
        for k in range(8):
            note = triad[k % 3] + 12 if k >= 3 else triad[k % 3]
            api("POST", f"/track/{piano}/note",
                {"tick": t0 + k * PPQ // 2, "duration": PPQ // 2, "key": note, "velocity": 0.4, "lyric": ""})
    # —— 真鼓点循环（SoundBug Loops，GM ch10）——
    LOOPS = "D:/SoundBug/Loops"
    def lay_drums(segment_start_bar, segment_bars, loop_file, loop_beats):
        """loop_beats: 循环节拍长（8拍=2小节 / 16拍=4小节）。"""
        repeats = max(segment_bars * 4 // loop_beats, 1)
        for n in loop_to_notes(f"{LOOPS}/{loop_file}", repeats, segment_start_bar * BAR):
            api("POST", f"/track/{drum_tr}/note",
                {"tick": n["tick"], "duration": n["dur"], "key": n["key"],
                 "velocity": n["velocity"], "lyric": ""})
    lay_drums(0, 4, "Drum-Intro-Drum-8beats/1.mid", 8)        # 前奏
    lay_drums(4, 12, "Drum-Verse-Drum-4beats/1.mid", 16)      # 主歌A
    lay_drums(12, 4, "Drum-Bridge-Drum-8beats/1.mid", 8)      # 预副歌
    lay_drums(16, 12, "Drum-Verse-Drum-4beats/1.mid", 16)     # 副歌
    lay_drums(28, 4, "Drum-Bridge-Drum-8beats/1.mid", 8)      # 间奏
    lay_drums(32, 8, "Drum-Verse-Drum-4beats/1.mid", 16)      # 主歌B
    lay_drums(40, 12, "Drum-Verse-Drum-4beats/1.mid", 16)     # 副歌2
    lay_drums(52, 4, "Drum-Outro-Drum-4beats/1.mid", 4)       # 尾奏
    # —— Bass 循环 ——
    for seg_start, seg_bars in [(4, 8), (16, 12), (32, 8), (40, 12)]:
        for n in loop_to_notes(f"{LOOPS}/Bass-Bass-Bass-16beats/1.mid",
                               max(seg_bars * 4 // 16, 1), seg_start * BAR):
            api("POST", f"/track/{bass_tr}/note",
                {"tick": n["tick"], "duration": n["dur"], "key": n["key"] - 12,
                 "velocity": n["velocity"], "lyric": ""})

    # ---------- 主唱旋律 ----------
    def write_vocals(track_id, start_bar, lyrics_block, scale, vel=0.9):
        bars = parse_lyrics(lyrics_block)
        all_notes = []
        for bi, sylls in enumerate(bars):
            bar = start_bar + bi
            notes = melody_notes(bar * BAR, sylls, scale)
            all_notes.extend(notes)
        for (tick, dur, key, lyric, leg) in all_notes:
            nid = api("POST", f"/track/{track_id}/note",
                      {"tick": tick, "duration": dur, "key": key, "lyric": lyric, "velocity": vel})["id"]
        return all_notes

    verse_a = write_vocals(lead, 4, LYRICS_VERSE_A, VERSE_SCALE_A)
    verse_a2 = write_vocals(lead, 8, LYRICS_VERSE_A, VERSE_SCALE_A)         # 主歌A 占满 8 小节
    prech = write_vocals(lead, 12, "星 光 在 前 方 闪 烁 | 我 的 心 开 始 滚 灼", PRECHORUS_SCALE)
    prech2 = write_vocals(lead, 14, "我 的 心 开 始 滚 灼 | 梦 想 在 夜 空 闪 烁", PRECHORUS_SCALE)
    # 副歌 16-28 = 12 小节：副歌词 4 小节 × 3 遍
    for rep in range(3):
        write_vocals(lead, 16 + rep * 4, LYRICS_CHORUS, CHORUS_SCALE, vel=1.0)
    verse_b = write_vocals(lead, 32, LYRICS_VERSE_B, VERSE_SCALE_B)
    verse_b2 = write_vocals(lead, 36, LYRICS_VERSE_B, VERSE_SCALE_B)        # 主歌B 占满 8 小节
    # 副歌2 40-52 同样 3 遍
    for rep in range(3):
        write_vocals(lead, 40 + rep * 4, LYRICS_CHORUS, CHORUS_SCALE, vel=1.0)
    outro = write_vocals(lead, 52, LYRICS_OUTRO, OUTRO_SCALE, vel=0.8)
    outro2 = write_vocals(lead, 54, LYRICS_OUTRO, OUTRO_SCALE, vel=0.7)   # 尾奏填满 52-56
    # 56-58 渐弱重复 + 58-60 纯伴奏收束
    outro3 = write_vocals(lead, 56, LYRICS_OUTRO, OUTRO_SCALE, vel=0.55)

    # ---------- 和声轨（副歌2：三度平行） ----------
    chorus2_notes = []
    for bi, sylls in enumerate(parse_lyrics(LYRICS_CHORUS)):
        chorus2_notes.extend(melody_notes((40 + bi * 4) * BAR, sylls, CHORUS_SCALE))
    for (tick, dur, key, lyric, leg) in chorus2_notes:
        api("POST", f"/track/{harm}/note",
            {"tick": tick, "duration": dur, "key": max(48, key - 4), "lyric": lyric,
             "velocity": 0.75, "legato": leg})

    # ---------- 表现力曲线 ----------
    total_tick = 60 * BAR
    def paint(param, segments):
        """segments: [(from,to,value)]"""
        for a, b, v in segments:
            api("POST", f"/track/{lead}/curve",
                {"param": param, "from_tick": a, "to_tick": b, "value": v})

    # pitch：副歌加宽颤音感（±分音的静态近似 → 用交替小段模拟 vibrato）
    vib = []
    for bar in range(16, 52):
        t0 = bar * BAR
        for k in range(16):  # 每小节 16 个正弦采样点 ±45 音分
            a = t0 + k * BAR // 16
            import math
            v = 45.0 * math.sin(2 * math.pi * k / 8)
            vib.append((a, a + BAR // 16, round(v, 1)))
    paint("pitch", vib)

    # energy：段落动态包络
    energy = [
        (0 * BAR, 4 * BAR, 0.35),        # 前奏轻（无歌声无妨，作用于轨）
        (4 * BAR, 12 * BAR, 0.55),       # 主歌 A
        (12 * BAR, 16 * BAR, 0.7),       # 预副歌渐强
        (16 * BAR, 28 * BAR, 0.9),       # 副歌
        (28 * BAR, 32 * BAR, 0.5),       # 间奏
        (32 * BAR, 40 * BAR, 0.6),       # 主歌 B
        (40 * BAR, 52 * BAR, 0.95),      # 副歌 2
        (52 * BAR, 56 * BAR, 0.6),       # 尾奏渐弱
        (56 * BAR, 60 * BAR, 0.35),
    ]
    paint("energy", energy)

    # air：主歌气声多、副歌扎实
    paint("air", [(4 * BAR, 16 * BAR, 0.35), (16 * BAR, 28 * BAR, 0.12),
                  (32 * BAR, 40 * BAR, 0.3), (40 * BAR, 52 * BAR, 0.1),
                  (52 * BAR, 60 * BAR, 0.4)])
    # tension：预副歌/副歌张力拉满
    paint("tension", [(12 * BAR, 16 * BAR, 0.8), (16 * BAR, 28 * BAR, 0.7),
                      (40 * BAR, 52 * BAR, 0.75)])
    # falsetto：尾奏假声收束
    paint("falsetto", [(52 * BAR, 60 * BAR, 0.5)])
    # gender：副歌略微亮一点
    paint("gender", [(16 * BAR, 52 * BAR, 0.62)])
    # velocity：整体冲程
    paint("velocity", [(0, total_tick, 0.85)])
    # dynamic：主输出包络（前奏/间奏压低歌声轨意义不大，但保持平滑）
    paint("dynamic", [(0, total_tick, 0.9)])

    # 和声轨简单 energy
    api("POST", f"/track/{harm}/curve",
        {"param": "energy", "from_tick": 40 * BAR, "to_tick": 52 * BAR, "value": 0.7})

    print("工程注入完成")
    print(json.dumps({"lead": lead, "harm": harm, "piano": piano, "chord": chord_tr}))


if __name__ == "__main__":
    build()
