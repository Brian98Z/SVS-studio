//! 8 通道表现力参数曲线层（契约 §2）。
//!
//! `base` (Baseline) 为只读先验；user/envelope 为加性偏移；direct 为稀疏绝对覆盖。
//! global 在覆盖之后叠加；NaN/缺失样本表示该层无贡献；最终按参数范围截断。

use crate::PARAMS;
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use serde::{Deserialize, Serialize};

/// pitch 通道使用音分范围，其余通道 0..1。
pub fn param_range(param: &str) -> (f32, f32) {
    if param == "pitch" {
        (-1200.0, 1200.0)
    } else {
        (0.0, 1.0)
    }
}

/// NaN 占位常量（IEEE-754 0x7FC00000，契约 §2）。
pub const GAP: f32 = f32::NAN;

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct ParamCurve {
    /// Base64(f32le) 或空串（全空）。
    #[serde(default)]
    pub base: String,
    #[serde(default)]
    pub user: String,
    #[serde(default)]
    pub envelope: String,
    #[serde(default)]
    pub direct: String,
    #[serde(default)]
    pub global: String,
}

impl ParamCurve {
    pub fn empty() -> Self {
        Self::default()
    }

    pub fn encode_f32(vals: &[f32]) -> String {
        let mut bytes = Vec::with_capacity(vals.len() * 4);
        for v in vals {
            bytes.extend_from_slice(&v.to_le_bytes());
        }
        B64.encode(bytes)
    }

    /// 解码 Base64 → f32 向量；非法输入返回空。
    pub fn decode_f32(b64: &str) -> Vec<f32> {
        if b64.is_empty() {
            return Vec::new();
        }
        match B64.decode(b64) {
            Ok(bytes) => bytes
                .chunks_exact(4)
                .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
                .collect(),
            Err(_) => Vec::new(),
        }
    }

    pub fn layer(&self, layer: &str) -> Option<&str> {
        match layer {
            "base" => Some(&self.base),
            "user" => Some(&self.user),
            "envelope" => Some(&self.envelope),
            "direct" => Some(&self.direct),
            "global" => Some(&self.global),
            _ => None,
        }
    }

    /// Baseline 不允许通过画笔修改。
    pub fn editable_layer(&mut self, layer: &str) -> Option<&mut String> {
        match layer {
            "user" => Some(&mut self.user),
            "envelope" => Some(&mut self.envelope),
            "direct" => Some(&mut self.direct),
            "global" => Some(&mut self.global),
            _ => None,
        }
    }

    /// direct 覆盖前三层，global 最后叠加；只在最终值上截断。
    pub fn effective(param: &str, len: usize) -> impl Fn(&Self) -> Vec<f32> {
        let (lo, hi) = param_range(param);
        move |c: &Self| {
            let layers = [&c.base, &c.user, &c.envelope, &c.direct, &c.global]
                .map(|s| Self::decode_f32(s));
            let sample = |layer: usize, i: usize| {
                layers[layer].get(i).copied().filter(|v| v.is_finite())
            };
            (0..len).map(|i| {
                let lower = || (0..3).map(|l| sample(l, i).unwrap_or(0.0)).sum::<f32>();
                (sample(3, i).unwrap_or_else(lower) + sample(4, i).unwrap_or(0.0))
                    .clamp(lo, hi)
            }).collect()
        }
    }
}

/// 8 通道曲线集合；缺失通道按空处理。
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct CurveBank {
    #[serde(flatten)]
    pub channels: std::collections::BTreeMap<String, ParamCurve>,
}

impl CurveBank {
    pub fn new() -> Self {
        Self::default()
    }

    /// 全 8 通道按契约序取克隆（缺失通道为空层）。
    pub fn ordered(&self) -> Vec<(&str, ParamCurve)> {
        PARAMS
            .iter()
            .map(|p| {
                let c = self.channels.get(*p).cloned().unwrap_or_default();
                (*p, c)
            })
            .collect()
    }

    /// 按通道和层固定排序，保留边界，避免拼接歧义及旧缓存命中。
    pub fn cache_key(&self) -> String {
        let channels: Vec<_> = PARAMS.iter().map(|p| {
            self.channels.get(*p).map(|c| [
                c.base.as_str(), c.user.as_str(), c.envelope.as_str(),
                c.direct.as_str(), c.global.as_str(),
            ]).unwrap_or([""; 5])
        }).collect();
        serde_json::to_string(&channels).expect("curve strings serialize")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_with_nan() {
        let vals = [0.0f32, 0.5, -0.25, 1.0, GAP, 65536.5];
        let b64 = ParamCurve::encode_f32(&vals);
        let dec = ParamCurve::decode_f32(&b64);
        assert_eq!(dec.len(), 6);
        assert!(dec[4].is_nan());
        for i in [0usize, 1, 2, 3, 5] {
            assert!((dec[i] - vals[i]).abs() < 1e-6);
        }
    }

    #[test]
    fn f32le_byte_order() {
        let b64 = ParamCurve::encode_f32(&[1.0]);
        let bytes = B64.decode(b64).unwrap();
        assert_eq!(bytes, 1.0f32.to_le_bytes());
    }

    #[test]
    fn effective_clamping() {
        let c = ParamCurve {
            base: ParamCurve::encode_f32(&[0.9]),
            user: ParamCurve::encode_f32(&[0.5]),
            ..Default::default()
        };
        let f = ParamCurve::effective("energy", 1);
        assert_eq!(f(&c), vec![1.0]);

        let p = ParamCurve {
            base: ParamCurve::encode_f32(&[800.0]),
            user: ParamCurve::encode_f32(&[600.0]),
            ..Default::default()
        };
        let f = ParamCurve::effective("pitch", 1);
        assert_eq!(f(&p), vec![1200.0]);
    }

    #[test]
    fn nan_user_means_no_offset() {
        let c = ParamCurve {
            base: ParamCurve::encode_f32(&[0.6]),
            user: ParamCurve::encode_f32(&[GAP]),
            ..Default::default()
        };
        let f = ParamCurve::effective("energy", 1);
        assert_eq!(f(&c), vec![0.6]);
    }

    #[test]
    fn direct_override_global_offset_and_sparse_holes() {
        let c = ParamCurve {
            base: ParamCurve::encode_f32(&[0.4, 0.4, GAP]),
            user: ParamCurve::encode_f32(&[0.3, 0.3, GAP]),
            envelope: ParamCurve::encode_f32(&[-0.2, -0.2, 0.5]),
            direct: ParamCurve::encode_f32(&[0.1, GAP]),
            global: ParamCurve::encode_f32(&[0.2, -0.1, GAP]),
        };
        let result = ParamCurve::effective("energy", 4)(&c);
        for (got, expected) in result.iter().zip([0.3, 0.4, 0.5, 0.0]) {
            assert!((got - expected).abs() < 1e-6);
        }
    }

    #[test]
    fn clamp_only_after_all_layers() {
        let c = ParamCurve {
            base: ParamCurve::encode_f32(&[0.9]),
            user: ParamCurve::encode_f32(&[0.5]),
            global: ParamCurve::encode_f32(&[-0.6]),
            ..Default::default()
        };
        assert!((ParamCurve::effective("energy", 1)(&c)[0] - 0.8).abs() < 1e-6);
    }

    #[test]
    fn cache_key_distinguishes_layers_and_channel_boundaries() {
        let mut bank = CurveBank::new();
        let mut changed = CurveBank::new();
        bank.channels.entry("pitch".into()).or_default().user = "AAAA".into();
        changed.channels.entry("energy".into()).or_default().user = "AAAA".into();
        assert_ne!(bank.cache_key(), changed.cache_key());
        changed = bank.clone();
        changed.channels.get_mut("pitch").unwrap().envelope = "AAAA".into();
        assert_ne!(bank.cache_key(), changed.cache_key());
    }

    #[test]
    fn old_project_layers_deserialize_and_roundtrip_new_layers() {
        let mut c: ParamCurve = serde_json::from_str(r#"{"base":"","user":""}"#).unwrap();
        assert_eq!(ParamCurve::effective("pitch", 2)(&c), vec![0.0, 0.0]);
        c.direct = ParamCurve::encode_f32(&[GAP, 300.0]);
        c.global = ParamCurve::encode_f32(&[20.0, -50.0]);
        let restored: ParamCurve = serde_json::from_str(&serde_json::to_string(&c).unwrap()).unwrap();
        assert_eq!(ParamCurve::effective("pitch", 2)(&restored), vec![20.0, 250.0]);
    }
}
