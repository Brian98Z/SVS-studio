//! ABC 导入自动编曲器：从人声主旋律 + 和弦标记生成完整编曲（和声/钢琴/贝斯/鼓/和弦轨）。
//!
//! 编排规则（乐理约束）：
//! - 和声：主旋律长音（≥ 1/8 音符）下三度跟随，落在和弦内音上；短音符不跟（避免浑浊）
//! - 钢琴：每拍和弦琶音（根音-五度-八度-十度），八度 4-5 区；无和弦小节沿用上一和弦
//! - 贝斯：每小节根音低两个八度，全音符；换和弦处重击
//! - 鼓：4/4 基本型 kick@1,3 / snare@2,4 / hihat 八分；仅在有伴奏轨时生成
//! - 和弦轨：谱面标记原样（lyric=和弦名, key=根音 MIDI）

use crate::phrase::Note;
use crate::project::Track;

/// 和弦质量：小/大/强力和弦
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Quality {
    Major,
    Minor,
    Five, // 无三度（m 或缺省标记如 "G5"）
}

/// 解析和弦名 → (根音 MIDI C4=60 起，质量)。支持 C/C#/Db/D… + m/maj/min/5 后缀。
pub fn parse_chord(name: &str) -> Option<(u8, Quality)> {
    let name = name.trim();
    if name.is_empty() {
        return None;
    }
    let bytes = name.as_bytes();
    let mut i = 0usize;
    let root = match bytes[0] {
        b'A'..=b'G' => {
            let base = match bytes[0] {
                b'C' => 0, b'D' => 2, b'E' => 4, b'F' => 5, b'G' => 7, b'A' => 9, _ => 11,
            };
            i += 1;
            let mut v = base;
            if i < bytes.len() && (bytes[i] == b'#' || bytes[i] == b'b') {
                v = if bytes[i] == b'#' { v + 1 } else { v + 11 };
                i += 1;
            }
            (v % 12) as u8
        }
        _ => return None,
    };
    let rest = &name[i..];
    let q = if rest.starts_with('m') && !rest.starts_with("maj") {
        Quality::Minor
    } else if rest.starts_with('5') {
        Quality::Five
    } else {
        Quality::Major
    };
    Some((60 + root, q))
}

/// 和弦三音（相对根音的半音数）
fn third(q: Quality) -> i32 {
    match q {
        Quality::Major => 4,
        Quality::Minor => 3,
        Quality::Five => 7, // 强力和弦用五度代替三度
    }
}

fn fifth() -> i32 {
    7
}

#[derive(Debug, Clone)]
pub struct ChordSlot {
    pub tick: u32,
    pub duration: u32,
    pub name: String,
    pub root: u8,
    pub quality: Quality,
}

/// 把 chords JSON（桥输出 [{tick,name}]）+ 每小节 tick 数 → 和弦时间轴（无标记小节延续前一和弦）
pub fn build_timeline(chords: &[(u32, String)], total_ticks: u32, bar_ticks: u32) -> Vec<ChordSlot> {
    let mut slots: Vec<ChordSlot> = Vec::new();
    let mut next = 0usize;
    let mut tick = 0u32;
    let mut last: Option<(u8, Quality, String)> = None;
    while tick < total_ticks.max(1) {
        // 该小节的新和弦
        while next < chords.len() && chords[next].0 <= tick {
            if let Some((root, q)) = parse_chord(&chords[next].1) {
                last = Some((root, q, chords[next].1.clone()));
            }
            next += 1;
        }
        let end = (tick + bar_ticks).min(total_ticks);
        if let Some((root, q, name)) = last.clone() {
            slots.push(ChordSlot { tick, duration: end - tick, name, root, quality: q });
        }
        tick += bar_ticks;
    }
    slots
}

fn mk_note(tick: u32, dur: u32, key: u8, velocity: f32) -> Note {
    Note {
        id: uuid::Uuid::new_v4().to_string(),
        tick,
        duration: dur,
        key,
        velocity,
        lyric: String::new(),
        phonemes: vec![],
        legato: false,
        head_ticks: 0,
        tail_ticks: 0,
        kind: "sung".into(),
        vibrato: None,
        attack_ms: None,
        decay_ms: None,
    }
}

/// 和声轨：主旋律下三度，长音跟随并吸附和弦内音
pub fn gen_harmony(melody: &[Note], chords: &[ChordSlot]) -> Vec<Note> {
    let mut out = Vec::new();
    for n in melody {
        if n.duration < 240 {
            continue; // 短音不跟
        }
        // 所在和弦
        let chord = chords.iter().find(|c| n.tick >= c.tick && n.tick < c.tick + c.duration);
        // 下三度（大三度/小三度视和弦质量），再吸附进和弦内音集
        let mut k = n.key as i32 - 3;
        if let Some(c) = chord {
            let chord_tones = [
                c.root as i32,
                c.root as i32 + third(c.quality),
                c.root as i32 + fifth(),
                c.root as i32 + 12,
                c.root as i32 + 12 + third(c.quality),
                c.root as i32 + 12 + fifth(),
            ];
            // k 与 k-1 中更靠近和弦内音的
            let dist = |x: i32| chord_tones.iter().map(|t| (x - t).abs()).min().unwrap_or(99);
            if dist(k - 1) < dist(k) {
                k -= 1;
            }
            // 吸附到最近和弦内音（±2 半音内）
            let (best, bd) = chord_tones.iter().fold((k, 99), |(bk, bdist), t| {
                let d = (k - t).abs();
                if d < bdist { (*t, d) } else { (bk, bdist) }
            });
            if bd <= 2 {
                k = best;
            }
        }
        if (0..=127).contains(&k) && k != n.key as i32 {
            // 和声歌词：与主唱同字（独立存储，可单独编辑/AI 重填）
            let mut h = mk_note(n.tick, n.duration, k as u8, 0.7);
            h.lyric = n.lyric.clone();
            out.push(h);
        }
    }
    out
}

/// 钢琴伴奏：每拍琶音 根-五-八-十（1 小节 4 拍 × 4 音）
pub fn gen_piano(chords: &[ChordSlot]) -> Vec<Note> {
    let mut out = Vec::new();
    for c in chords {
        let beat = c.duration / 4.max(1) as u32;
        if beat == 0 {
            continue;
        }
        // 中音区（C3-C5）
        let root = {
            let mut r = c.root as i32 - 12;
            while r < 48 {
                r += 12;
            }
            r.min(72) as u8
        };
        let pattern: [i32; 4] = [0, fifth(), 12, 12 + third(c.quality)];
        for (i, off) in pattern.iter().enumerate() {
            let t = c.tick + i as u32 * beat;
            if t >= c.tick + c.duration {
                break;
            }
            let k = (root as i32 + off) as u8;
            out.push(mk_note(t, beat - 8, k, 0.55));
        }
    }
    out
}

/// 贝斯：每小节根音低两个八度；小节中点加五度过渡
pub fn gen_bass(chords: &[ChordSlot]) -> Vec<Note> {
    let mut out = Vec::new();
    for c in chords {
        let root = {
            let mut r = c.root as i32 - 24;
            while r < 28 {
                r += 12;
            }
            r.min(52) as u8
        };
        let half = c.duration / 2;
        out.push(mk_note(c.tick, half - 8, root, 0.8));
        if half > 0 {
            let f = (root as i32 + fifth()) as u8;
            out.push(mk_note(c.tick + half, c.duration - half - 8, f, 0.6));
        }
    }
    out
}

/// 鼓：4/4 基本型。GM 鼓 key：36=kick, 38=snare, 42=closed hh
pub fn gen_drums(total_ticks: u32, bar_ticks: u32) -> Vec<Note> {
    let mut out = Vec::new();
    let beat = bar_ticks / 4.max(1) as u32;
    if beat == 0 {
        return out;
    }
    let mut t = 0u32;
    while t < total_ticks {
        for b in 0..4 {
            let bt = t + b * beat;
            if bt >= total_ticks {
                break;
            }
            if b == 0 || b == 2 {
                out.push(mk_note(bt, 60, 36, 0.9)); // kick
            } else {
                out.push(mk_note(bt, 60, 38, 0.7)); // snare
            }
            // hihat 八分
            out.push(mk_note(bt, 30, 42, 0.4));
            if bt + beat / 2 < total_ticks {
                out.push(mk_note(bt + beat / 2, 30, 42, 0.3));
            }
        }
        t += bar_ticks;
    }
    out
}

/// 和弦轨 notes：lyric=和弦名, key=根音
pub fn gen_chord_notes(chords: &[ChordSlot]) -> Vec<Note> {
    chords
        .iter()
        .map(|c| Note {
            id: uuid::Uuid::new_v4().to_string(),
            tick: c.tick,
            duration: c.duration,
            key: c.root,
            velocity: 1.0,
            lyric: c.name.clone(),
            phonemes: vec![],
            legato: false,
            head_ticks: 0,
            tail_ticks: 0,
            kind: "sung".into(),
            vibrato: None,
            attack_ms: None,
            decay_ms: None,
        })
        .collect()
}

/// 一次性生成全部伴奏轨（和声/钢琴/贝斯/鼓/和弦）
pub fn arrange(melody: &[Note], chords: &[(u32, String)], total_ticks: u32, bar_ticks: u32) -> Vec<Track> {
    let timeline = build_timeline(chords, total_ticks, bar_ticks);
    let mk = |kind: crate::project::TrackKind, name: &str, notes: Vec<Note>| {
        let mut t = Track::new(kind, name);
        t.notes = notes;
        t
    };
    vec![
        {
            let mut h = mk(crate::project::TrackKind::Sing, "和声", gen_harmony(melody, &timeline));
            h.singer_id = "diffsinger.qixuan".into();
            h.volume = 0.6; // 和声伴唱音量
            h
        },
        mk(crate::project::TrackKind::Instrument, "钢琴伴奏", gen_piano(&timeline)),
        mk(crate::project::TrackKind::Instrument, "贝斯", gen_bass(&timeline)),
        mk(crate::project::TrackKind::Instrument, "鼓机", gen_drums(total_ticks, bar_ticks)),
        mk(crate::project::TrackKind::Chord, "和弦", gen_chord_notes(&timeline)),
    ]
}
