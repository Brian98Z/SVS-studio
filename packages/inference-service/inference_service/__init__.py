"""SVS 推理服务：ONNX SVS 管道 + 内置 DSP 回退音源。

架构（契约 §4/§5）：
- HTTP 服务 127.0.0.1:8760，由 Rust server 以子进程方式拉起。
- /synth_phrase: 按句合成，LRU 缓存（键 = 契约 §5 哈希）。
- 管道：mel2dur → mel2pitch → acoustic → vocoder；models/ 下存在对应 ONNX
  即启用真模型管道，否则自动落入 DSP 源-滤波回退（保证端到端可出声）。
- 分档 Padding 桶（避坑 5）：128/256/512/1024，避免变长张量频繁重建。

运行: python -m inference_service.service_runner [--port 8760]
"""
