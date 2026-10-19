"""句子级 LRU 音频缓存 + 缓存管理。

- 容量：128 句 / 512MB 双上限（契约 §5），先淘汰最旧。
- 值：f32 PCM (np.ndarray)；统计 hit/miss 供 MCP status 读取。
- 二级磁盘缓存：render_ds 的整轨渲染同时落盘（.pcm + .meta.json），
  server/推理进程重启后仍命中——播放不重复渲染（用户要求：渲染好即本地保存）。
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

MAX_ITEMS = 128
MAX_BYTES = 512 * 1024 * 1024
DISK_DIR = Path(__file__).resolve().parent.parent / "cache_pcm"


class AudioCache:
    def __init__(self, max_items: int = MAX_ITEMS, max_bytes: int = MAX_BYTES):
        self._d: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self._bytes = 0
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.hits = 0
        self.misses = 0
        self.disk_hits = 0
        try:
            DISK_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def _disk_paths(self, key: str):
        safe = "".join(c if c.isalnum() else "-" for c in key)[:80]
        return DISK_DIR / f"{safe}.pcm", DISK_DIR / f"{safe}.meta.json"

    def get(self, key: str) -> np.ndarray | None:
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                self.hits += 1
                return self._d[key]
        # 内存未命中 → 查磁盘（不持锁，读文件可能慢）
        pcm_path, meta_path = self._disk_paths(key)
        try:
            if pcm_path.exists() and meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                raw = pcm_path.read_bytes()
                pcm = np.frombuffer(raw, dtype=np.float32).copy()
                self.disk_hits += 1
                self.put(key, pcm, to_disk=False)  # 回填内存
                return pcm
        except Exception:  # noqa: BLE001 — 磁盘坏档按未命中处理
            pass
        with self._lock:
            self.misses += 1
        return None

    def put(self, key: str, audio: np.ndarray, to_disk: bool = True) -> None:
        with self._lock:
            if key in self._d:
                self._bytes -= self._d[key].nbytes
                del self._d[key]
            self._d[key] = audio
            self._bytes += audio.nbytes
            self._evict()
        # 磁盘层：整轨渲染（render_ds 传入 to_disk=True）落盘，重启后命中
        if to_disk:
            try:
                pcm_path, meta_path = self._disk_paths(key)
                pcm_path.write_bytes(np.asarray(audio, dtype=np.float32).tobytes())
                meta_path.write_text(json.dumps({
                    "hash": key, "samples": int(len(audio)),
                    "sample_rate": 24000, "kind": "render_ds_track",
                }, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001 — 磁盘满/权限失败不影响内存缓存
                pass

    def invalidate(self, hashes: list[str] | None = None, all: bool = False) -> int:
        with self._lock:
            if all:
                n = len(self._d)
                self._bytes = 0
                self._d.clear()
                return n
            n = 0
            for h in hashes or []:
                if h in self._d:
                    self._bytes -= self._d[h].nbytes
                    del self._d[h]
                    n += 1
            return n

    def _evict(self) -> None:
        while len(self._d) > self.max_items or (self._bytes > self.max_bytes and len(self._d) > 1):
            _, v = self._d.popitem(last=False)
            self._bytes -= v.nbytes

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._d),
                "bytes": self._bytes,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }
