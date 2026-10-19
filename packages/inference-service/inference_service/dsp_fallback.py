"""DSP 回退音源：源-滤波歌声合成（无 ONNX 模型时的端到端回退）。

实现要点：
- 源：多谐波加法合成（基频 + 谐波列，1/k 幅度衰减 + 轻微奇偶不对称），
  pitch 通道（音分偏移）实时调制瞬时频率 → 滑音/颤音可听。
- 呼吸噪声：air 通道控制与源相加的带限白噪声比例。
- 滤波：两个时变共振峰（元音近似 /a/ /i/ /u/ 由元音音素选择）双二阶带通级联，
  gender 通道整体平移共振峰中心频率。
- 包络：energy 通道幅度包络；tension 调谐波噪声混合比；falsetto 减少谐波数
  并加气声；velocity 决定辅音段爆发噪声。
- 音符间 5ms 等功率交叉淡化 + 首尾 256 样本余弦窗（避坑 4 防爆音）。

这不是神经声码器质量的歌声，但保证：无模型环境下系统全链路可运行、
参数通道全部可闻、且 ONNX 模型放入 models/ 后自动切换真管道。
"""
from __future__ import annotations

import math

import numpy as np

SR = 24000
HOP = 512
FADE = 256  # 余弦窗样本数（避坑 4）
XFADE = int(0.005 * SR)  # 音符拼接交叉淡化 5ms

# 元音共振峰近似（Hz）：F1/F2
VOWEL_FORMANTS = {
    "a": (800, 1200), "i": (280, 2300), "u": (320, 800),
    "e": (500, 1900), "o": (480, 900),
}


def _default_formant(phonemes: list[str]) -> tuple[float, float]:
    for p in phonemes:
        for v, f in VOWEL_FORMANTS.items():
            if v in p.lower():
                return f
    return VOWEL_FORMANTS["a"]


def _midi_to_hz(key: int) -> float:
    return 440.0 * 2 ** ((key - 69) / 12)


def _biquad_bandpass(x: np.ndarray, f0: np.ndarray, q: float) -> np.ndarray:
    """时变中心频率带通：每样本按当前 f0 重算 RBJ 系数并更新状态。"""
    n = len(x)
    out = np.zeros(n)
    s1 = s2 = 0.0
    for i in range(n):
        w0 = 2 * math.pi * f0[i] / SR
        sinw = math.sin(w0)
        cosw = math.cos(w0)
        alpha = sinw / (2 * q)
        a0 = 1 + alpha
        b0 = alpha / a0
        b1 = 0.0
        b2 = -alpha / a0
        a1 = -2 * cosw / a0
        a2 = (1 - alpha) / a0
        xn = x[i]
        yn = b0 * xn + b1 * s1 + b2 * s2 - a1 * s1 - a2 * s2
        s2 = s1
        s1 = yn
        out[i] = yn
    return out * 6.0  # 带通能量补偿


def synth_note(
    key: int,
    dur_samples: int,
    velocity: float,
    phonemes: list[str],
    pitch_cents: np.ndarray | None,
    energy: np.ndarray | None,
    air: np.ndarray | None,
    tension: np.ndarray | None,
    falsetto: np.ndarray | None,
    gender: np.ndarray | None,
    timbre: str = "voice",
) -> np.ndarray:
    """timbre: voice（共振峰歌声）| piano（衰减谐波）| drum（噪声打击）| bass（低音正弦+过驱动）"""
    """合成单音符波形（f32, [-1,1]）。参数数组长度可小于音符，按最近邻伸展。"""
    n = dur_samples
    f0_base = _midi_to_hz(key)

    def stretch(arr: np.ndarray | None, default: float) -> np.ndarray:
        if arr is None or len(arr) == 0:
            return np.full(n, default, dtype=np.float32)
        idx = np.clip((np.arange(n) * len(arr) / n).astype(int), 0, len(arr) - 1)
        out = arr[idx].astype(np.float32)
        # NaN 空洞（未绘制区间）按中性默认值填充，防止 NaN 扩散到整段波形
        out = np.where(np.isnan(out), np.float32(default), out)
        return out

    cents = stretch(pitch_cents, 0.0)
    energy_c = stretch(energy, 0.8)
    air_c = stretch(air, 0.1)
    tension_c = stretch(tension, 0.5)
    falsetto_c = stretch(falsetto, 0.0)
    gender_c = stretch(gender, 0.5)

    # ---------- 乐器音色分支（SoundBug 式多轨编曲） ----------
    if timbre == "drum":
        return _synth_drum(key, n, velocity)
    if timbre == "bass":
        return _synth_bass(key, n, velocity, energy_c)
    if timbre == "piano":
        return _synth_piano(key, n, velocity, energy_c)

    # 瞬时频率：f0 * 2^(cents/1200)
    inst_f0 = f0_base * 2.0 ** (cents / 1200.0)
    phase = 2 * math.pi * np.cumsum(inst_f0) / SR

    # 谐波列：幅度 1/k；falsetto 抬高、tension 改变奇偶平衡
    nharm_full = 24
    nharm = np.maximum(4, (nharm_full * (1.0 - 0.6 * falsetto_c))).astype(int)
    src = np.zeros(n, dtype=np.float64)
    for k in range(1, nharm_full + 1):
        active = k <= nharm
        amp = (1.0 / k) * np.where(active, 1.0, 0.0)
        tilt = (1.0 + tension_c * (0.3 if k % 2 else -0.3))
        src += amp * tilt * np.sin(k * phase)

    # 气声噪声（air + falsetto 抬升）——宽带直混，避免被共振峰带通滤除
    noise_level = np.clip(air_c * 0.6 + falsetto_c * 0.3, 0.0, 0.95)
    rng = np.random.default_rng(key * 7919 + n)
    noise = (rng.standard_normal(n) * noise_level).astype(np.float64)

    # 共振峰滤波：gender 全局迁移 F1/F2（±20%）
    f1_0, f2_0 = _default_formant(phonemes)
    shift = 2.0 ** ((gender_c - 0.5) * 0.8)  # gender 0..1 → ±~1.37x
    f1 = np.clip(f1_0 * shift, 80, SR / 2 - 200)
    f2 = np.clip(f2_0 * shift, 80, SR / 2 - 200)

    # 谐波分量过共振峰；气声噪声宽带直混（否则被带通滤光）
    harm = _biquad_bandpass(src * (1.0 - 0.4 * falsetto_c), f1, 6.0)
    harm = harm + 0.7 * _biquad_bandpass(harm, f2, 8.0)
    out = harm + noise * 3.0

    # 能量包络
    out *= energy_c * (0.4 + 0.6 * velocity)
    # 归一防削波：谐波叠加可能超 ±1
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0.99:
        out *= 0.99 / peak
    return out.astype(np.float32)


def apply_fades(buf: np.ndarray, fade: int = FADE) -> np.ndarray:
    """首尾余弦平方窗，防切片爆音（避坑 4）。"""
    n = len(buf)
    f = min(fade, n // 2)
    if f > 0:
        w = 0.5 * (1 - np.cos(np.pi * np.arange(f) / f))
        buf[:f] *= w
        buf[n - f:] *= w[::-1]
    return buf


# ---------------- GM 鼓机音色（key 对应 GM 打击乐映射） ----------------

def _synth_drum(key: int, n: int, velocity: float) -> np.ndarray:
    rng = np.random.default_rng(key * 104729 + n)
    t = np.arange(n) / SR
    # GM: 36 底鼓 / 38 军鼓 / 42,44 闭/开镲 / 49 崩镲 / 39 手拍 / 45 中鼓 / 51 Ride
    if key in (35, 36):      # Kick：低频扫频 + 短促
        f = 120 * np.exp(-t * 30) + 45
        out = np.sin(2 * math.pi * np.cumsum(f) / SR) * np.exp(-t * 18)
    elif key in (38, 40):    # Snare：噪声 + 200Hz 体
        noise = rng.standard_normal(n) * np.exp(-t * 28)
        body = np.sin(2 * math.pi * 190 * t) * np.exp(-t * 30) * 0.5
        out = (noise * 0.8 + body)
    elif key in (42, 44, 46):  # Hi-hat：高通噪声，闭合短开合长
        noise = rng.standard_normal(n)
        hp = np.diff(noise, prepend=0.0)  # 一阶高通
        decay = 55 if key == 42 else 12
        out = hp * np.exp(-t * decay)
    elif key in (49, 55, 51, 59):  # Cymbal/Ride：金属感（多频不谐和簇）
        out = sum(np.sin(2 * math.pi * f * t + rng.random() * 6)
                  for f in (321, 477, 631, 893)) / 4
        out *= np.exp(-t * (4 if key != 51 else 2.2))
        out += rng.standard_normal(n) * np.exp(-t * 20) * 0.4
    else:                    # Tom 类（41,43,45,47,48,50）：频率下扫
        f0 = 120 + (key % 12) * 18
        f = f0 * np.exp(-t * 12) + 60
        out = np.sin(2 * math.pi * np.cumsum(f) / SR) * np.exp(-t * 14)
    out *= 0.85 * (0.3 + 0.7 * velocity)
    out = dsp_fallback_apply_fades(out)
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0.99:
        out *= 0.99 / peak
    return out.astype(np.float32)


def dsp_fallback_apply_fades(buf: np.ndarray, fade: int = 64) -> np.ndarray:
    n = len(buf)
    f = min(fade, n // 2)
    if f > 0:
        w = 0.5 * (1 - np.cos(np.pi * np.arange(f) / f))
        buf[:f] *= w
        buf[n - f:] *= w[::-1]
    return buf


def _synth_bass(key: int, n: int, velocity: float, energy_c: np.ndarray) -> np.ndarray:
    """Bass：基频+方波味奇次谐波 + 低通，衰减包络。"""
    f0 = _midi_to_hz(max(key, 28))  # 钳到低音区
    t = np.arange(n) / SR
    out = np.zeros(n)
    ph = 2 * math.pi * f0 * t
    # 起音相位从 0 开始（避免任意相位跳变杂音）
    for k in (1, 3, 5, 7):
        out += np.sin(ph * k) / k
    out = np.convolve(out, np.ones(16) / 16, mode="same")
    out *= np.exp(-t * 2.2)
    # ADSR 包络：8ms 起音 / 尾部 30ms 释放——消除首尾杂音
    env = np.ones(n)
    a = min(int(0.008 * SR), n // 4)
    r = min(int(0.030 * SR), n // 4)
    env[:a] = np.linspace(0, 1, a)
    env[n - r:] *= np.linspace(1, 0, r)
    out *= env
    out *= energy_c * (0.4 + 0.6 * velocity)
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0.99:
        out *= 0.99 / peak
    return out.astype(np.float32)


def _synth_piano(key: int, n: int, velocity: float, energy_c: np.ndarray) -> np.ndarray:
    """Piano：谐波列 + 指数衰减 + 轻微失谐双弦。"""
    f0 = _midi_to_hz(key)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for detune, amp in ((0.9993, 0.5), (1.0007, 0.5)):
        ph = 2 * math.pi * f0 * detune * t
        for k in range(1, 11):
            out += amp * np.sin(ph * k) / (k ** 1.3) * np.exp(-t * (2.0 + k * 0.35))
    attack = np.minimum(t / 0.004, 1.0)  # 4ms 起音
    out *= attack
    out *= np.exp(-t * 1.4)
    # 尾部 20ms 释放（自然衰减已小，防截断咔哒）
    r = min(int(0.020 * SR), n // 4)
    out[n - r:] *= np.linspace(1, 0, r)
    out *= (0.4 + 0.6 * velocity)
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0.99:
        out *= 0.99 / peak
    return out.astype(np.float32)


def render_phrase_silence(dur_samples: int) -> np.ndarray:
    return np.zeros(dur_samples, dtype=np.float32)
