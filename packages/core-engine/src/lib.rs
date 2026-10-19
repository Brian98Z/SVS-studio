//! SVS Studio 核心引擎：乐理坐标、参数曲线层、短语切片、历史栈。
//!
//! 契约见 docs/CONTRACT.md。所有常量与公式以契约为单一事实来源。

pub mod arrange;
pub mod curve;
pub mod history;
pub mod phrase;
pub mod project;
pub mod timeline;

/// 每四分音符脉冲数（契约 §1）
pub const PPQ: u32 = 480;
/// 合成采样率（契约 §1）
pub const SAMPLE_RATE: u32 = 24_000;
/// 8 大参数通道，枚举序即索引序（契约 §2）
pub const PARAMS: [&str; 8] = [
    "pitch", "energy", "air", "tension", "falsetto", "gender", "velocity", "dynamic",
];
/// 哈希字段分隔符（契约 §5）
pub const HASH_SEP: &str = "\u{1f}";
/// 短语切片的最小休止间隙：1 拍（契约 §5）
pub const PHRASE_GAP_TICKS: u32 = PPQ;
