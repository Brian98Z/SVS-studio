//! Tick ↔ ms ↔ 小节 三向坐标系统（契约 §1）。
//!
//! 时间换算必须走 Tempo 表分段积分：找到 tick 左侧最近锚点，
//! 累计前段时长再加余量。禁止全局单一 BPM 公式（避坑 3）。

use crate::{PPQ, SAMPLE_RATE};
use serde::{Deserialize, Serialize};

/// 有序 Tempo 变更点表；首点必须是 tick=0。
pub type TempoList = Vec<TempoPoint>;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "lowercase")]
pub enum TempoCurve {
    #[default]
    Step,
    Linear,
}

fn is_step(curve: &TempoCurve) -> bool {
    *curve == TempoCurve::Step
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct TempoPoint {
    pub tick: u32,
    pub bpm: f64,
    #[serde(default, skip_serializing_if = "is_step")]
    pub curve: TempoCurve,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct TimeSigPoint {
    pub tick: u32,
    pub numerator: u32,
    pub denominator: u32,
}

/// Rejects tempo tables that cannot define a monotonic tick/time mapping.
pub fn validate_tempos(tempos: &[TempoPoint]) -> Result<(), &'static str> {
    if tempos.is_empty() || tempos[0].tick != 0 {
        return Err("tempo table must start at tick 0");
    }
    for (index, point) in tempos.iter().enumerate() {
        if !point.bpm.is_finite() || point.bpm <= 0.0 {
            return Err("tempo BPM must be finite and positive");
        }
        if let Some(next) = tempos.get(index + 1) {
            if next.tick <= point.tick {
                return Err("tempo ticks must be strictly increasing");
            }
        }
    }
    Ok(())
}

fn segment_ms(point: &TempoPoint, next: Option<&TempoPoint>, ticks: f64) -> f64 {
    let factor = 60_000.0 / PPQ as f64;
    let Some(next) = next.filter(|_| point.curve == TempoCurve::Linear) else {
        return ticks * factor / point.bpm;
    };
    let span = (next.tick - point.tick) as f64;
    let slope = (next.bpm - point.bpm) / span;
    if slope.abs() < 1e-12 {
        ticks * factor / point.bpm
    } else {
        factor * ((point.bpm + slope * ticks) / point.bpm).ln() / slope
    }
}

/// tempo tick 位置换算到毫秒；线性曲线按 BPM 关于 tick 的解析积分。
pub fn tick_to_ms(tempos: &[TempoPoint], tick: u32) -> f64 {
    let mut ms = 0.0;
    for (i, p) in tempos.iter().enumerate() {
        let next = tempos.get(i + 1);
        let end = next.map_or(f64::INFINITY, |n| n.tick as f64);
        let at = tick as f64;
        let seg_end = at.min(end);
        if seg_end > p.tick as f64 {
            ms += segment_ms(p, next, seg_end - p.tick as f64);
        }
        if at < end {
            return ms;
        }
    }
    ms
}

/// 毫秒换算回最近 tick；使用同一解析积分的逆函数并四舍五入。
pub fn ms_to_tick(tempos: &[TempoPoint], ms: f64) -> u32 {
    if tempos.is_empty() || ms <= 0.0 {
        return 0;
    }
    let factor = 60_000.0 / PPQ as f64;
    let mut elapsed = 0.0;
    for (i, p) in tempos.iter().enumerate() {
        let next = tempos.get(i + 1);
        let span = next.map(|n| (n.tick - p.tick) as f64);
        let segment = span.map_or(f64::INFINITY, |ticks| segment_ms(p, next, ticks));
        if ms < elapsed + segment {
            let remaining = ms - elapsed;
            let ticks = if let Some(next) = next.filter(|_| p.curve == TempoCurve::Linear) {
                let slope = (next.bpm - p.bpm) / (next.tick - p.tick) as f64;
                if slope.abs() < 1e-12 {
                    remaining * p.bpm / factor
                } else {
                    p.bpm * (remaining * slope / factor).exp_m1() / slope
                }
            } else {
                remaining * p.bpm / factor
            };
            return (p.tick as f64 + ticks).round().clamp(0.0, u32::MAX as f64) as u32;
        }
        elapsed += segment;
    }
    tempos.last().map(|p| p.tick).unwrap_or(0)
}

/// 毫秒换算到合成样本数。
pub fn ms_to_samples(ms: f64) -> u64 {
    (ms / 1000.0 * SAMPLE_RATE as f64).round() as u64
}

/// tick 换算到合成样本数（按 Tempo 表积分）。
pub fn tick_to_samples(tempos: &[TempoPoint], tick: u32) -> u64 {
    ms_to_samples(tick_to_ms(tempos, tick))
}

/// `Measure:Beat:Tick` 文本坐标 → 绝对 tick（契约 §1 离散坐标）。
///
/// 拍号决定每小节拍数；denominator 以 4 分音符为基准折算每拍 tick：
/// 4/4 → 每拍 480；6/8 → 每拍 240。
pub fn mbt_to_tick(timesigs: &[TimeSigPoint], measure: u32, beat: u32, tick: u32) -> u32 {
    let mut acc_tick = 0u64;
    let mut measures_passed = 0u64;
    let target = measure as u64;
    for (i, ts) in timesigs.iter().enumerate() {
        let next_tick = timesigs.get(i + 1).map(|n| n.tick).unwrap_or(u32::MAX) as u64;
        let beats_per_measure = beats_of(ts.numerator, ts.denominator);
        // 该拍号段覆盖的小节数 = 段 tick 长度 / 小节 tick 长度
        let seg_len = if next_tick == u64::MAX {
            u64::MAX
        } else {
            next_tick - ts.tick as u64
        };
        let measure_ticks = beats_per_measure * PPQ as u64;
        let seg_measures = if seg_len == u64::MAX {
            u64::MAX
        } else {
            seg_len / measure_ticks.max(1).max(0)
        };
        if measures_passed + seg_measures > target {
            let in_measure = target - measures_passed;
            let beat_ticks = beat_ticks_of(ts.denominator);
            return (acc_tick + in_measure * measure_ticks + (beat as u64) * beat_ticks + tick as u64) as u32;
        }
        measures_passed += seg_measures;
        acc_tick += seg_measures * measure_ticks;
    }
    acc_tick as u32
}

fn beats_of(numerator: u32, denominator: u32) -> u64 {
    // 每小节折合的四分音符数
    numerator as u64 * 4 / denominator.max(1) as u64
}

fn beat_ticks_of(denominator: u32) -> u64 {
    // 每拍 tick 数：以四分音符 = PPQ 折算
    PPQ as u64 * 4 / denominator.max(1) as u64
}

/// 绝对 tick → `(measure, beat, tick)`。
pub fn tick_to_mbt(timesigs: &[TimeSigPoint], tick: u32) -> (u32, u32, u32) {
    let mut acc_tick = 0u64;
    let mut measure = 0u64;
    for ts in timesigs {
        let next_tick = timesigs
            .iter()
            .find(|n| n.tick > ts.tick)
            .map(|n| n.tick)
            .unwrap_or(u32::MAX) as u64;
        let measure_ticks = beats_of(ts.numerator, ts.denominator) * PPQ as u64;
        let seg_end = if next_tick == u64::MAX { u64::MAX } else { next_tick };
        if (tick as u64) < seg_end {
            let rel = tick as u64 - acc_tick;
            return (
                (measure + rel / measure_ticks) as u32,
                (rel % measure_ticks / beat_ticks_of(ts.denominator)) as u32,
                (rel % beat_ticks_of(ts.denominator)) as u32,
            );
        }
        let seg_measures = (seg_end - acc_tick) / measure_ticks.max(1);
        measure += seg_measures;
        acc_tick += seg_measures * measure_ticks;
    }
    (measure as u32, 0, 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tempos() -> TempoList {
        vec![
            TempoPoint { tick: 0, bpm: 120.0, curve: TempoCurve::Step },
            TempoPoint { tick: 960, bpm: 60.0, curve: TempoCurve::Step },
            TempoPoint { tick: 1440, bpm: 240.0, curve: TempoCurve::Step },
        ]
    }

    #[test]
    fn tempo_integration() {
        assert_eq!(tick_to_ms(&tempos(), 0), 0.0);
        assert_eq!(tick_to_ms(&tempos(), 480), 500.0);
        assert_eq!(tick_to_ms(&tempos(), 960), 1000.0);
        assert_eq!(tick_to_ms(&tempos(), 1440), 2000.0);
        assert_eq!(tick_to_ms(&tempos(), 1441), 2000.0 + 60000.0 / (240.0 * 480.0));
    }

    #[test]
    fn linear_ramp_exact_integral_and_inverse() {
        let ramp = [
            TempoPoint { tick: 0, bpm: 60.0, curve: TempoCurve::Linear },
            TempoPoint { tick: 960, bpm: 120.0, curve: TempoCurve::Step },
        ];
        let half_ms = 60_000.0 / 480.0 * 960.0 * 2.0_f64.ln() / 60.0;
        assert!((tick_to_ms(&ramp, 960) - half_ms).abs() < 1e-9);
        for tick in [0, 1, 120, 480, 959, 960, 1200] {
            assert_eq!(ms_to_tick(&ramp, tick_to_ms(&ramp, tick)), tick, "tick={tick}");
        }
    }

    #[test]
    fn tempo_roundtrip() {
        for t in [0u32, 1, 100, 480, 959, 960, 1200, 1439, 1440, 2000, 9999] {
            assert_eq!(ms_to_tick(&tempos(), tick_to_ms(&tempos(), t)), t, "tick={t}");
        }
    }

    #[test]
    fn tempo_bounds_and_validation() {
        assert_eq!(tick_to_ms(&[], 100), 0.0);
        assert_eq!(ms_to_tick(&[], 100.0), 0);
        assert_eq!(ms_to_tick(&tempos(), -1.0), 0);
        assert!(validate_tempos(&tempos()).is_ok());
        for invalid in [
            vec![TempoPoint { tick: 1, bpm: 120.0, curve: TempoCurve::Step }],
            vec![TempoPoint { tick: 0, bpm: 0.0, curve: TempoCurve::Step }],
            vec![TempoPoint { tick: 0, bpm: f64::INFINITY, curve: TempoCurve::Step }],
            vec![TempoPoint { tick: 0, bpm: 120.0, curve: TempoCurve::Step }, TempoPoint { tick: 0, bpm: 60.0, curve: TempoCurve::Step }],
        ] {
            assert!(validate_tempos(&invalid).is_err());
        }
    }

    #[test]
    fn old_step_contract_omits_default_curve() {
        let point: TempoPoint = serde_json::from_str(r#"{"tick":0,"bpm":120.0}"#).unwrap();
        assert_eq!(point.curve, TempoCurve::Step);
        assert_eq!(serde_json::to_string(&point).unwrap(), r#"{"tick":0,"bpm":120.0}"#);
    }

    #[test]
    fn mbt_44() {
        let ts = vec![TimeSigPoint { tick: 0, numerator: 4, denominator: 4 }];
        assert_eq!(mbt_to_tick(&ts, 0, 0, 0), 0);
        assert_eq!(mbt_to_tick(&ts, 2, 1, 0), 2 * 4 * 480 + 480);
        assert_eq!(mbt_to_tick(&ts, 1, 3, 120), 4 * 480 + 3 * 480 + 120);
        assert_eq!(tick_to_mbt(&ts, 4 * 480 + 3 * 480 + 120), (1, 3, 120));
    }

    #[test]
    fn mbt_68() {
        let ts = vec![
            TimeSigPoint { tick: 0, numerator: 4, denominator: 4 },
            TimeSigPoint { tick: 4 * 480, numerator: 6, denominator: 8 },
        ];
        assert_eq!(mbt_to_tick(&ts, 1, 0, 0), 4 * 480);
        assert_eq!(mbt_to_tick(&ts, 2, 1, 0), 4 * 480 + 1440 + 240);
        assert_eq!(tick_to_mbt(&ts, 4 * 480 + 1440 + 240), (2, 1, 0));
    }

    #[test]
    fn samples() {
        assert_eq!(ms_to_samples(1000.0), 24_000);
        assert_eq!(tick_to_samples(&[TempoPoint { tick: 0, bpm: 120.0, curve: TempoCurve::Step }], 480), 12_000);
    }
}
