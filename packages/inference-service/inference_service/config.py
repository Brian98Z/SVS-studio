"""集中配置：所有外部依赖路径/端口在此唯一声明。

优先级：环境变量 > 默认值。工程可整体搬迁，不依赖运行位置。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---- svs-studio 工程根（本文件位于 packages/inference-service/inference_service/）----
SVS_ROOT = Path(__file__).resolve().parent.parent.parent

# ---- abc2diffsinger 插件（ABC 解析 / DiffSinger 音源 / GPU venv）----
ABC2VOCALS_DIR = Path(os.environ.get(
    "SVS_ABC2VOCALS_DIR",
    r"F:/OMP/projects/项目与插件/abc2diffsinger",
))
_voicebank_default = ABC2VOCALS_DIR / 'voicebanks' / 'Qixuan_v2.7.0_DiffSinger_OpenUtau'
VOICEBANK = (Path(os.environ['SVS_VOICEBANK']) if os.environ.get('SVS_VOICEBANK')
             else _voicebank_default if os.environ.get('SVS_DESKTOP_MODE') != '1' and _voicebank_default.is_dir() else None)
GPU_PYTHON = Path(os.environ.get(
    "SVS_GPU_PYTHON",
    str(ABC2VOCALS_DIR / "runtime" / "venv" / "Scripts" / "python.exe"),
))

# ---- LLM（AI 歌词）——key 存插件 config/llm.json（沿用插件安全约定）----
LLM_CONFIG_FILE = ABC2VOCALS_DIR / "config" / "llm.json"

# ---- 端口 ----
SERVER_PORT = int(os.environ.get("SVS_PORT", "8761"))
INFERENCE_PORT = int(os.environ.get("SVS_INFERENCE_PORT", "8760"))
