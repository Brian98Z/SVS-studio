# SVS Studio — 跨语言共享契约 v1

单一事实来源。前后端、CLI、MCP、推理服务全部按本文件实现；改动需同步所有消费方。

## 1. 坐标与单位

| 常量 | 值 | 说明 |
|---|---|---|
| `PPQ` | 480 | Pulses Per Quarter Note，全系统唯一 |
| 音高 | MIDI 0–127 | 60 = C4 |
| 参数采样步长 | 1 tick | 曲线通道按 tick 索引 |
| 单帧样本数 `hop_size` | 512 | 声学帧移（24kHz, 46.4ms/帧）|
| 合成采样率 | 24000 Hz | f32le 单声道 |

- `tick→ms`：分段积分。`ms(tick) = t_i_ms + (tick − tick_i) · 60000 / (bpm_i · PPQ)`，`i` 为 `tick` 左侧最近 Tempo 点（tick_i, bpm_i, t_i_ms）。
- `ms→tick`：同一分段线性表的逆映射。
- 小节坐标：优先累计整小节 tick（按 TimeSig 表），再映射时间；禁止用全局单一 BPM 公式。

## 2. 参数通道（8 个，枚举序即索引序）

```
pitch energy air tension falsetto gender velocity dynamic
```

- 每通道持久化五层：`base`（Baseline，只读先验）、`user`、`envelope`、`direct`、`global`。旧工程缺失新增字段按空串读取；`base` 字段不改名。
- 空串 / 缺失 / 非有限样本 = 该层无贡献；序列化为 IEEE-754 `f32le` 稠密小端流 → 标准 Base64，`NaN`（0x7FC00000）表示空洞。
- `lower = finite(base,0) + finite(user,0) + finite(envelope,0)`；`effective = clamp((direct 为有限值 ? direct : lower) + finite(global,0))`。只在最后截断；pitch 为 ±1200 音分，其余 0..1。
- User / Envelope / Global 支持负偏移（非 pitch 输入 −1..1）；Direct 为绝对值。`global` 是按 tick 的最终偏移层，不是全工程主音量。
- DSP 在五层全部空洞的位置保留音源自身默认值；曲线 API 的空洞有效值仍为 0。显式绘制 0 与擦除不同。

## 3. 工程文档模型（单文件 JSON，`.svsproj`）

```jsonc
{
  "schema": 1,
  "name": "未命名工程",
  "ppq": 480,
  "sample_rate": 24000,
  "tempos": [{ "tick": 0, "bpm": 120.0 }],
  "time_signatures": [{ "tick": 0, "numerator": 4, "denominator": 4 }],
  "tracks": [
    {
      "id": "uuid-v4",
      "kind": "sing",                    // sing | instrument | audio | chord
      "name": "主唱",
      "singer_id": "builtin.fallback",   // sing 轨
      "instrument": "builtin.piano",     // instrument 轨
      "volume": 1.0, "pan": 0.0,         // pan −1..1
      "mute": false, "solo": false,
      "params": ["pitch","energy","air","tension","falsetto","gender","velocity","dynamic"],
      "notes": [
        {
          "id": "uuid-v4", "tick": 0, "duration": 480,
          "key": 60, "velocity": 1.0, "lyric": "你好",
          "phonemes": ["sil","ao","iao"],       // g2p 产物，可空
          "legato": false, "head_ticks": 0, "tail_ticks": 0
        }
      ],
      "curves": {                        // 8 通道；base 由推理引擎回填
        "pitch": { "base": "<b64>", "user": "<b64>", "envelope": "<b64>", "direct": "<b64>", "global": "<b64>" },
        "energy": { "base": "", "user": "<b64>", "envelope": "", "direct": "", "global": "" }
      }
    }
  ],
  "audio_clips": [
    { "id": "uuid", "track_id": "uuid", "path": "sample.wav",
      "start_tick": 0, "gain": 1.0, "fade_in_ms": 0, "fade_out_ms": 0 }
  ]
}
```

## 4. 推理服务 HTTP API（inference-service，127.0.0.1:8760）

| Method | Path | 请求体 | 响应 |
|---|---|---|---|
| GET | `/health` | — | `{"ok":true,"mode":"onnx"|"dsp","sample_rate":24000}` |
| POST | `/synth_phrase` | PhraseSpec | `{"ok":true,"audio":"<b64 f32le>","sample_rate":24000,"cache":"hit"|"miss","elapsed_ms":n}` |
| POST | `/invalidate` | `{"hashes":["..."]}` 或 `{"all":true}` | `{"ok":true,"removed":n}` |

**PhraseSpec**（短语切片，见 §5 哈希规则）：

```jsonc
{
  "hash": "sha256hex-64",
  "notes": [Note],          // 已含 phonemes
  "offset_ticks": 0,        // 短语首音符绝对 tick
  "tempos": [{"tick":0,"bpm":120.0}],
  "singer": "builtin.fallback",
  "curves": { "pitch": { "base": "", "user": "<b64>", "envelope": "", "direct": "", "global": "" } }
}
```

曲线数组使用轨道绝对 tick；`notes.tick` 是短语相对 tick，须加 `offset_ticks` 后取曲线窗口与速度积分。当前 DSP 支持八通道五层；velocity 是 DSP 电平控制，dynamic 为独立后级增益。当前 DiffSinger 与通用 ONNX 适配器不支持外部曲线：存在有限样本时返回 `422 UNSUPPORTED_CONTROLS`，不静默忽略；DiffSinger 轨内变速同样明确拒绝。

## 5. 短语切片与哈希（server 与推理服务共用规则）

- 切片：同一 sing 轨内，音符排序后，若 `next.tick − (cur.tick+cur.duration) ≥ 480`（1 拍 @120BPM）则切分；legato 音符跨切片归属前句。
- 句哈希：`sha256(singer + SEP + 每音符规范紧凑 JSON + SEP + 排序后的参数名 JSON + SEP + curve_cache_key + SEP + sample_rate + SEP + model_tag)`；SEP 为 `\u001f`，音符规范字段为 tick/duration/key/velocity/lyric/phonemes/legato（不含 id）。
- `curve_cache_key` 为按上述八通道顺序的 `[base,user,envelope,direct,global]` 字符串数组 JSON，加 `:`，再加按 tick,bpm 字段顺序序列化的速度表 JSON。模型标签升级为 `dsp-layers-2` / `diffsinger-layers-2`，避免命中旧语义缓存。
- 任意层或速度表编辑都使缓存失效；当前粒度为整轨曲线键，因此一个层编辑会重算该轨全部短句，未声称句级局部失效。
- LRU 容量：128 句 / 512MB 上限，先淘汰最旧。

## 6. 桌面端 ↔ server HTTP/WebSocket

REST（同 CLI 命令一一对应，见 §8 命令集）+ WS `/ws` 推送：

```jsonc
{ "type": "engine.status", "synth_state": "idle|rendering", "phrases_done": 3, "phrases_total": 7 }
{ "type": "transport.time", "playing": true, "tick": 960, "ms": 2000.0 }
{ "type": "project.saved", "path": "..." }
```

曲线 REST：
- `POST /api/track/{id}/curve`：`{param,layer?,from_tick,to_tick,value}`，layer 默认 user；区间 `[from_tick,to_tick)`，value=null 擦除为 NaN。base 禁止写入；tick 上限 10,000,000。
- `GET /api/track/{id}/curve/{param}?len=1920`：返回 `param,len,base_b64,user_b64,envelope_b64,direct_b64,global_b64,effective_b64`；len 上限 10,000,000。
- 推理失败必须传播详情，不能生成缺失人声的“成功”WAV；渲染期间编辑后 `/api/pcm` 保持过期状态。
- `GET /api/inference/hardware`：返回原生 DiffSinger 已加载会话的 `preference`、`provider`、`selected_providers` 和 `sessions`；无实际会话时不得把 provider 安装清单等同于运行证明。`SVS_INFERENCE_DEVICE=cuda` 要求实际加载 CUDA，否则报错；会话可同时包含 CPU provider 处理不支持的算子。
- `POST /api/inference/music-ai/vocal-to-midi`：输入 `{audio_base64}`，返回 480 PPQ、120 BPM 的 `notes`、`midi_base64`、`device` 和实际 ONNX `providers`；音符坐标按秒映射到当前工程速度图。CUDA 不可用时失败，不静默改为 CPU。
- `POST /api/inference/music-ai/stems`：输入 `{audio_base64,model?}`，本地 Demucs 返回 `device`、`model_device`、`sample_rate` 和各路 WAV `stems`。GPU 证据必须来自模型参数/输入设备及实际运行，不能只看 torch 安装状态。
- AI 主题作曲与伴奏使用配置的聊天模型服务；不属于本地 GPU 推理承诺。解码、混音和音符后处理可在 CPU 执行。

## 7. CLI 命令集（`svs-cli <cmd> [args]`，REST 语义）

```
new / open <path> / save [path] / save-as <path>
track add <kind> [name] / track list / track remove <id> / track set <id> --vol --pan --mute --solo --singer
note add <track> --tick --dur --key [--lyric] / note list <track> / note move|resize|del <id> ...
curve paint <track> --param pitch --layer user|envelope|direct|global --from 0 --to 1920 --value 200
curve clear <track> --param energy --layer direct --from 0 --to 1920   // 擦除而非写零
synth run [--track <id>]        // 触发全轨切片推理（服务端已有缓存则跳过）
transport play|stop|seek <ms>
render wav <out.wav> [--from-ms --to-ms]   // 离线并轨导出
export midi <out.mid> / import midi <in.mid>
metronome on|off
```

## 8. MCP 工具集（svs-mcp，stdio FastMCP）

| 工具 | 参数 | 行为 |
|---|---|---|
| `svs_health` | — | server+inference 存活检查 |
| `svs_project_open` | `path` | 打开工程 |
| `svs_project_save` | `path?` | 保存 |
| `svs_note_add` | `track, tick, dur, key, lyric` | 加音符（触发 g2p+切片）|
| `svs_note_list` | `track` | 列音符 |
| `svs_curve_paint` | `track,param,from_tick,to_tick,value(float/null),layer=user` | 指定层绘制或擦除 |
| `svs_curve_read` | `track,param` | 读取五层与有效曲线 |
| `svs_synth` | `track?` | 合成并返回缓存统计 |
| `svs_render` | `out_path, from_ms?, to_ms?` | 渲染 WAV |
| `svs_transport` | `action(play/stop/seek), ms?` | 走带 |
| `svs_get_status` | — | 工程名/轨道/时长/缓存命中率 |

## 9. G2P 词典格式（packages/g2p-engine/dicts/）

```
# mandarin.tsv  汉字<TAB>拼音音素序列
你	n i3
好	h ao3
# cmu_fragment.tsv  英词<TAB>音素（ARPAbet，重音数字去除）
hello	hh ah l ow
# kana.tsv  假名<TAB>音素
は	h a
```

多音字：`polyphone.tsv`（词组优先最长匹配）；假名走 romaji 表；英文先查词典，未命中退化为字母直拼表。

## 10. 错误码（HTTP status + body `{"error": "code", "detail": "..."}`）

```
400 BAD_REQUEST / 404 NOT_FOUND / 409 PROJECT_NOT_OPEN / 422 UNSUPPORTED_CONTROLS (推理服务) / 500 INTERNAL / 502 SYNTH_FAILED / 503 SYNTH_FAILED
```
