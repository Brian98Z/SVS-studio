//! 工程文档模型（契约 §3）：多轨道、音符、曲线、音频剪辑。

use crate::curve::CurveBank;
use crate::phrase::Note;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum TrackKind {
    Sing,
    Instrument,
    Audio,
    Chord,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub struct Track {
    pub id: String,
    pub kind: TrackKind,
    pub name: String,
    /// sing 轨：歌手声线 ID
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub singer_id: String,
    #[serde(default)]
    pub voice: serde_json::Value,
    /// instrument 轨：乐器 ID
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub instrument: String,
    #[serde(default = "one")]
    pub volume: f32,
    /// −1..1
    #[serde(default)]
    pub pan: f32,
    #[serde(default)]
    pub mute: bool,
    #[serde(default)]
    pub solo: bool,
    /// SoundBug 式轨道配色 (#rrggbb)。
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub colour: String,
    #[serde(default)]
    pub notes: Vec<Note>,
    #[serde(default)]
    pub curves: CurveBank,
    #[serde(default)]
    pub fx: Vec<serde_json::Value>,
    #[serde(default)]
    pub sends: Vec<serde_json::Value>,
    #[serde(default)]
    pub bus: String,
}

fn one() -> f32 {
    1.0
}

impl Track {
    pub fn new(kind: TrackKind, name: &str) -> Self {
        Self {
            id: Uuid::new_v4().to_string(),
            kind,
            name: name.to_string(),
            singer_id: if kind == TrackKind::Sing {
                "builtin.fallback".into()
            } else {
                String::new()
            },
            instrument: String::new(),
            voice: serde_json::json!({}),
            volume: 1.0,
            pan: 0.0,
            mute: false,
            solo: false,
            colour: String::new(),
            notes: Vec::new(),
            curves: CurveBank::new(),
            fx: Vec::new(),
            sends: Vec::new(),
            bus: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AudioClip {
    pub id: String,
    pub track_id: String,
    pub path: String,
    pub start_tick: u32,
    #[serde(default = "one")]
    pub gain: f32,
    #[serde(default)]
    pub fade_in_ms: f64,
    #[serde(default)]
    pub fade_out_ms: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Project {
    pub schema: u32,
    pub name: String,
    pub ppq: u32,
    pub sample_rate: u32,
    /// 主控输出音量 0..1（SoundBug MASTERVOLUME）。
    #[serde(default = "one")]
    pub master_volume: f32,
    /// 节拍器开关与音量（SoundBug CLICKTRACK）。
    #[serde(default)]
    pub metronome: bool,
    #[serde(default = "default_click")]
    pub metronome_level: f32,
    pub tempos: Vec<crate::timeline::TempoPoint>,
    pub time_signatures: Vec<crate::timeline::TimeSigPoint>,
    pub tracks: Vec<Track>,
    #[serde(default)]
    pub audio_clips: Vec<AudioClip>,
    #[serde(default)]
    pub mixer: serde_json::Value,
}

fn default_click() -> f32 {
    0.6
}

impl Default for Project {
    fn default() -> Self {
        Self {
            schema: 1,
            name: "未命名工程".into(),
            ppq: crate::PPQ,
            sample_rate: crate::SAMPLE_RATE,
            master_volume: 1.0,
            metronome: false,
            metronome_level: 0.6,
            tempos: vec![crate::timeline::TempoPoint { tick: 0, bpm: 120.0, curve: crate::timeline::TempoCurve::Step }],
            time_signatures: vec![crate::timeline::TimeSigPoint {
                tick: 0,
                numerator: 4,
                denominator: 4,
            }],
            tracks: Vec::new(),
            audio_clips: Vec::new(),
            mixer: serde_json::json!({}),
        }
    }
}

impl Project {
    pub fn new(name: &str) -> Self {
        Self { name: name.into(), ..Default::default() }
    }

    pub fn track(&self, id: &str) -> Option<&Track> {
        self.tracks.iter().find(|t| t.id == id || t.name == id)
    }

    pub fn track_mut(&mut self, id: &str) -> Option<&mut Track> {
        self.tracks.iter_mut().find(|t| t.id == id || t.name == id)
    }

    /// 轨道最后音符结束 tick（工程总长参考）。
    pub fn track_end_tick(&self, track: &Track) -> u32 {
        track.notes.iter().map(|n| n.tick + n.duration).max().unwrap_or(0)
    }

    pub fn end_tick(&self) -> u32 {
        self.tracks.iter().map(|t| self.track_end_tick(t)).max().unwrap_or(0)
    }

    /// solo 激活时非 solo 轨静音（混音规则）。
    pub fn track_audible(&self, track: &Track) -> bool {
        if track.mute {
            return false;
        }
        let any_solo = self.tracks.iter().any(|t| t.solo);
        !any_solo || track.solo
    }

    pub fn to_json(&self) -> String {
        serde_json::to_string_pretty(self).unwrap_or_default()
    }

    pub fn from_json(s: &str) -> Result<Self, serde_json::Error> {
        serde_json::from_str(s)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_project_json_roundtrip() {
        let p = Project::new("测试");
        let s = p.to_json();
        let p2 = Project::from_json(&s).unwrap();
        assert_eq!(p2.name, "测试");
        assert_eq!(p2.ppq, 480);
        assert_eq!(p2.tempos[0].bpm, 120.0);
    }

    #[test]
    fn solo_mutes_others() {
        let mut p = Project::new("t");
        p.tracks.push(Track::new(TrackKind::Sing, "a"));
        p.tracks.push(Track::new(TrackKind::Sing, "b"));
        p.tracks[1].solo = true;
        assert!(!p.track_audible(&p.tracks[0]));
        assert!(p.track_audible(&p.tracks[1]));
        p.tracks[1].solo = false;
        assert!(p.track_audible(&p.tracks[0]));
    }
}
