"""MIDI 循环导入器：解析标准 MIDI（format 0/1）→ (tick, dur, key, vel) 音符列表。

参考 D:/SoundBug/Loops（GM 鼓 ch10 / Bass 循环，division 9600）。
供 build_song.py 直接把专业鼓点/贝斯循环注入 instrument 轨。
"""
from __future__ import annotations
import struct


def _varlen(data: bytes, i: int) -> tuple[int, int]:
    v = 0
    while True:
        b = data[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, i


def parse_midi(path: str) -> dict:
    """返回 {division, notes:[{tick,dur,key,vel,chan}], tempo_bpm}"""
    d = open(path, "rb").read()
    assert d[:4] == b"MThd", "not midi"
    fmt, ntrk, division = struct.unpack(">HHH", d[8:14])
    notes = []
    tempo_bpm = 120.0
    pos = 14
    while pos < len(d) - 8:
        if d[pos:pos+4] != b"MTrk":
            break
        tlen = struct.unpack(">I", d[pos+4:pos+8])[0]
        body = d[pos+8:pos+8+tlen]
        pos += 8 + tlen

        tick = 0
        i = 0
        running = 0
        active: dict[int, tuple[int, int]] = {}  # key -> (start_tick, vel)
        while i < len(body):
            delta, i = _varlen(body, i)
            tick += delta
            st = body[i]
            if st < 0x80:
                st = running  # running status
            else:
                i += 1
                running = st
            if st == 0xFF:
                mt = body[i]; i += 1
                ln, i = _varlen(body, i)
                if mt == 0x51 and ln == 3:
                    us = (body[i] << 16) | (body[i+1] << 8) | body[i+2]
                    tempo_bpm = 60_000_000 / us
                i += ln
            elif st in (0xF0, 0xF7):
                ln, i = _varlen(body, i)
                i += ln
            elif 0x80 <= st < 0x90:  # note off
                key, vel = body[i], body[i+1]; i += 2
                if key in active:
                    s, v = active.pop(key)
                    notes.append({"tick": s, "dur": tick - s, "key": key, "vel": v / 127,
                                  "chan": st & 0x0F})
            elif 0x90 <= st < 0xA0:  # note on
                key, vel = body[i], body[i+1]; i += 2
                if vel > 0:
                    active[key] = (tick, vel)
                else:
                    if key in active:
                        s, v = active.pop(key)
                        notes.append({"tick": s, "dur": tick - s, "key": key, "vel": v / 127,
                                      "chan": st & 0x0F})
            elif 0xC0 <= st < 0xE0:
                i += 1
            elif st < 0xF0:
                i += 2
    return {"division": division, "notes": notes, "tempo_bpm": tempo_bpm, "format": fmt}


def loop_to_notes(path: str, repeats: int, start_tick: int, ppq: int = 480) -> list[dict]:
    """把循环 MIDI 重复 repeats 次铺到 start_tick 起。division 自动折算到 ppq。"""
    m = parse_midi(path)
    loop_len = max((n["tick"] + n["dur"] for n in m["notes"]), default=0)
    if loop_len == 0:
        return []
    k = ppq / m["division"]  # tick 缩放
    out = []
    for r in range(repeats):
        off = start_tick + r * round(loop_len * k)
        for n in m["notes"]:
            out.append({
                "tick": off + round(n["tick"] * k),
                "dur": max(round(n["dur"] * k), 10),
                "key": n["key"],
                "velocity": round(n["vel"], 3),
                "chan": n["chan"],
            })
    return out


if __name__ == "__main__":
    import glob, os
    for f in sorted(glob.glob("D:/SoundBug/Loops/*/*.mid"))[:6]:
        m = parse_midi(f)
        end = max((n["tick"]+n["dur"] for n in m["notes"]), default=0)
        drums = sum(1 for n in m["notes"] if n["chan"] == 9)
        print(f"{os.path.basename(os.path.dirname(f))}/{os.path.basename(f)}: "
              f"div={m['division']} notes={len(m['notes'])} (ch10:{drums}) "
              f"loop={end/m['division']:.2f}拍 bpm={m['tempo_bpm']:.1f}")
