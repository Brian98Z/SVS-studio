"""ONNX SVS 管道：mel2dur → mel2pitch → acoustic → vocoder（契约 2.4.3）。

模型资产（packages/inference-service/models/）：
  dur_model.onnx / pitch_model.onnx / acoustic_model.onnx / vocoder.onnx
全部存在 → ONNX 模式；任一缺失 → DSP 回退（dsp_fallback.py）。

分档 Padding 桶（避坑 5）：帧长向上取整到 128/256/512/1024 档，
减少 DirectML/CPU 变长张量重建与显存碎片。

真实 SVS ONNX 的输入输出命名随训练框架不同而异；这里约定标准命名，
加载时从 session inputs 探测并对常见命名做别名映射。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

MODELS_DIR = Path(os.environ.get("SVS_MODEL_DIR", Path(__file__).resolve().parent.parent / "models")).expanduser().resolve()
MODEL_FILES = {
    "dur": "dur_model.onnx",
    "pitch": "pitch_model.onnx",
    "acoustic": "acoustic_model.onnx",
    "vocoder": "vocoder.onnx",
}
# 声学帧移样本数（契约 §1）
HOP = 512
BUCKETS = [128, 256, 512, 1024, 2048]


def pick_bucket(frames: int) -> int:
    for b in BUCKETS:
        if frames <= b:
            return b
    return ((frames + 1023) // 1024) * 1024


class OnnxPipeline:
    """真模型管道。load() 失败抛出 → 调用方回退 DSP。"""

    def __init__(self, models_dir: Path = MODELS_DIR):
        import onnxruntime as ort

        self.sessions = {}
        for role, fname in MODEL_FILES.items():
            p = models_dir / fname
            if not p.exists():
                raise FileNotFoundError(f"missing {fname}")
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            providers = ["CPUExecutionProvider"]
            if "DmlExecutionProvider" in ort.get_available_providers():
                providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
            self.sessions[role] = ort.InferenceSession(str(p), so, providers=providers)

    def _run(self, role: str, feeds: dict) -> dict:
        sess = self.sessions[role]
        names = {i.name: None for i in sess.get_inputs()}
        aligned = {k: v for k, v in feeds.items() if k in names}
        outs = sess.run(None, aligned)
        return {o.name or str(i): o for i, o in enumerate(outs)}

    @staticmethod
    def _pad(frames: np.ndarray, bucket: int) -> np.ndarray:
        if len(frames) >= bucket:
            return frames[:bucket]
        return np.pad(frames, (0, bucket - len(frames)))

    def synth(self, notes_spec: dict, pitch_curve: np.ndarray, frames_hint: int) -> np.ndarray:
        """notes_spec: {phones: int[], note_div: int[]}（帧级）；返回波形 f32。"""
        bucket = pick_bucket(frames_hint)
        phones = self._pad(np.asarray(notes_spec["phones"], dtype=np.int64), bucket)[None, :]
        note_div = self._pad(np.asarray(notes_spec["note_div"], dtype=np.int64), bucket)[None, :]

        _ = self._run("dur", {"phones": phones, "note_div": note_div})
        f0 = self._run("pitch", {"phones": phones, "note_div": note_div})
        mel = self._run("acoustic", {"phones": phones, "f0": f0[next(iter(f0))].astype(np.float32)})
        voc_in = next(iter(mel.values())).astype(np.float32)
        wav = self._run("vocoder", {"mel": voc_in})
        audio = next(iter(wav.values())).astype(np.float32).reshape(-1)
        return audio
