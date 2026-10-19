//! 音符结构与短语切片/哈希（契约 §5）。

use crate::{HASH_SEP, PHRASE_GAP_TICKS};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Note {
    pub id: String,
    pub tick: u32,
    pub duration: u32,
    /// MIDI 0..127
    pub key: u8,
    /// 0..1
    pub velocity: f32,
    #[serde(default)]
    pub lyric: String,
    /// G2P 产物音素序列，可空。
    #[serde(default)]
    pub phonemes: Vec<String>,
    #[serde(default)]
    pub legato: bool,
    /// 辅音保护区起点偏移（契约 2.2.3）。
    #[serde(default)]
    pub head_ticks: u32,
    #[serde(default)]
    pub tail_ticks: u32,
    #[serde(default = "default_note_kind")]
    pub kind: String,
    #[serde(default)]
    pub vibrato: Option<Vibrato>,
    #[serde(default)]
    pub attack_ms: Option<f32>,
    #[serde(default)]
    pub decay_ms: Option<f32>,
}

fn default_note_kind() -> String { "sung".to_owned() }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Vibrato {
    pub depth_cents: f32,
    pub frequency_hz: f32,
    pub delay_ms: f32,
    #[serde(default)]
    pub onset_ms: f32,
    #[serde(default)]
    pub release_ms: f32,
}


/// 按起始 tick 排序的音符集。
pub fn sort_notes(notes: &mut [Note]) {
    notes.sort_by_key(|n| (n.tick, n.key));
}

/// 休止符切片（避坑 1）：间隙 ≥ PHRASE_GAP_TICKS 即切分；
/// legato 音符不作为新句起点（跨间隙归属前句）。
pub fn split_phrases(notes: &[Note]) -> Vec<Vec<&Note>> {
    let mut sorted: Vec<&Note> = notes.iter().collect();
    sorted.sort_by_key(|n| (n.tick, n.key));

    let mut groups: Vec<Vec<&Note>> = Vec::new();
    let mut cur: Vec<&Note> = Vec::new();
    let mut max_end = 0u32;
    for n in sorted {
        let cut = !cur.is_empty()
            && n.tick.saturating_sub(max_end) >= PHRASE_GAP_TICKS
            && !n.legato;
        if cut {
            groups.push(std::mem::take(&mut cur));
            max_end = 0;
        }
        max_end = max_end.max(n.tick.saturating_add(n.duration));
        cur.push(n);
    }
    if !cur.is_empty() {
        groups.push(cur);
    }
    groups
}

/// 句哈希（契约 §5）：singer + 逐音符规范 JSON + 曲线 user 拼接 + SR + 模型标签。
pub fn phrase_hash(
    singer: &str,
    notes: &[&Note],
    curves_user_concat: &str,
    sample_rate: u32,
    model_tag: &str,
) -> String {
    let mut h = Sha256::new();
    h.update(singer.as_bytes());
    h.update(HASH_SEP);
    for n in notes {
        // 与 Python 参考实现一致的紧凑规范 JSON（键排序）
        let mut canonical = serde_json::json!({
            "tick": n.tick,
            "duration": n.duration,
            "key": n.key,
            "velocity": n.velocity,
            "lyric": n.lyric,
            "phonemes": n.phonemes,
            "legato": n.legato,
        });
        if n.kind != "sung" || n.vibrato.is_some() || n.attack_ms.is_some() || n.decay_ms.is_some() {
            canonical["kind"] = serde_json::json!(n.kind);
            canonical["vibrato"] = serde_json::json!(n.vibrato);
            canonical["attack_ms"] = serde_json::json!(n.attack_ms);
            canonical["decay_ms"] = serde_json::json!(n.decay_ms);
        }
        let s = serde_json::to_string(&canonical).unwrap_or_default();
        h.update(s.as_bytes());
        h.update(HASH_SEP);
    }
    // 参数序数组（Python 侧输出 ["air",...] 排序后的 JSON）
    let mut params: Vec<&str> = crate::PARAMS.to_vec();
    params.sort_unstable();
    let pjson = serde_json::to_string(&params).unwrap_or_default();
    h.update(pjson.as_bytes());
    h.update(HASH_SEP);
    h.update(curves_user_concat.as_bytes());
    h.update(HASH_SEP);
    h.update(sample_rate.to_string().as_bytes());
    h.update(HASH_SEP);
    h.update(model_tag.as_bytes());
    hex(&h.finalize())
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn note(tick: u32, dur: u32, key: u8, lyric: &str, legato: bool) -> Note {
        Note {
            id: format!("n{tick}"),
            tick,
            duration: dur,
            key,
            velocity: 1.0,
            lyric: lyric.into(),
            phonemes: vec![],
            legato,
            head_ticks: 0,
            tail_ticks: 0,
            kind: "sung".into(),
            vibrato: None,
            attack_ms: None,
            decay_ms: None,
        }
    }
    #[test]
    fn slicing_on_rest() {
        let notes = vec![
            note(0, 480, 60, "你", false),
            note(480, 480, 62, "好", false),
            note(1920, 480, 64, "世", false),
        ];
        let g = split_phrases(&notes);
        assert_eq!(g.iter().map(|g| g.len()).collect::<Vec<_>>(), vec![2, 1]);
    }

    #[test]
    fn legato_crosses_gap() {
        let notes = vec![
            note(0, 480, 60, "你", false),
            note(1920, 480, 64, "啊", true), // legato 跨 1440 间隙
        ];
        let g = split_phrases(&notes);
        assert_eq!(g.len(), 1);
    }

    #[test]
    fn overlap_checks_maximum_phrase_end() {
        let notes = vec![
            note(0, 1920, 60, "長", false),
            note(240, 120, 64, "短", false),
            note(1440, 480, 67, "覆盖", false),
        ];
        let groups = split_phrases(&notes);
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].len(), 3);
    }

    #[test]
    fn hash_changes_with_user_curve() {
        let notes = vec![note(0, 480, 60, "你", false)];
        let refs: Vec<&Note> = notes.iter().collect();
        let h1 = phrase_hash("s", &refs, "AAA", 24000, "dsp");
        let h2 = phrase_hash("s", &refs, "BBB", 24000, "dsp");
        let h3 = phrase_hash("s2", &refs, "AAA", 24000, "dsp");
        assert_ne!(h1, h2);
        assert_ne!(h1, h3);
        assert_eq!(h1, phrase_hash("s", &refs, "AAA", 24000, "dsp"));
        assert_eq!(h1.len(), 64);
    }
}
