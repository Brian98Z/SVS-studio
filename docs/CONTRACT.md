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
- 同模型speaker Blend采用copy-on-write：复制四组件reader后，必须同时复制并重绑定`pred_duration.dsdur`、`pred_pitch.dspitch`、`pred_variance.dsvariance`、`pred_acoustic.ds_acoustic`；前三者的`input_processor.vb_reader`亦指向复制reader。共享ONNX模型/会话，不修改原缓存speaker列表，不允许混合别名落回默认speaker。

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
- Windows Tauri 页面来源 `http://tauri.localhost` 可跨源访问本机server；CORS仅允许这一精确来源与GET/POST/PATCH/DELETE及Content-Type请求头，不允许任意网站来源。PCM响应自定义头可由该来源读取。

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

## 11. VST3 效果配置与持续试听

轨道 `fx`、总线 `effects` 和 `mixer.master.effects` 共用有序效果数组。VST3 项：

```json
{"type":"vst3","enabled":false,"path":"plugins/Effect.vst3","parameters":{"output_gain_db":-6},"block_frames":4096,"tail_seconds":0.5}
```

- `path` 默认使用项目内 `plugins/Effect.vst3`，也接受插件目录内相对路径；相对路径不得逃出插件根。源码默认项目根 `plugins/`，可用 `SVS_PROJECT_ROOT` 或 `SVS_PLUGIN_DIR` 指定位置。已有绝对路径仍可读，支持 `.vst3` 单文件及目录 bundle；默认只扫描项目插件目录，不自动加载原生代码。
- `parameters` 仅保存原始参数键和原始数值、布尔或字符串枚举值。中文显示字典为 `apps/desktop/src/audio/effect-labels.zh-CN.json`，不改写工程键、插件预设或二进制；未知参数保留原文。
- 可选`state_base64`为Pedalboard原生`preset_data`（VST3 `.vstpreset`），不是参数JSON；缺失/null表示无预设。严格Base64、解码上限4MiB，校验VST3 v1头、little-endian 64位List偏移、chunk边界；载入前比较32字节class ID，异类预设拒绝。仅在隔离worker恢复，再叠加`parameters`。预设只包含插件自行序列化的数据，不携带外部采样/文件，不代表运行中DSP历史或任意版本兼容。
- 预设与参数须作为一对保存/恢复，混音标准化不得丢弃任何已验证参数。真实WET预设未包含所选mode，Dragonfly写入参数后未处理时捕获状态可能仍是旧值；后续参数覆盖是必需行为，不对插件原生状态做猜测性修改或强行处理编辑器实例。预设独立往返不代表设置完整保留。
- 离线接口的 `block_frames` 为 1–65536 整数，`tail_seconds` 为 0–10 秒；输出追加明确请求的静音输入尾部。离线每次调用使用独立实例，调用内保持状态。
- 离线`process_stereo`按处理后`reported_latency_samples`（0–10秒整数）去除起始延迟，减去宿主已从返回PCM省略的起始样本数以避免双重补偿；补送零输入排空末尾，最终仍为原输入加显式尾音的帧数。渲染期间报告延迟变化报错，不静默错位。轨道/总线/Master均使用同一离线处理路径；此项不补偿插件未报告/误报延迟，不改变持续试听或显式混响尾音预算。
- 原生加载、检查、调参与离线处理在 spawned worker 中执行，离线默认单次60秒超时；持续会话另用5秒worker调用上限。崩溃和超时返回错误，不在主服务内执行插件。
- Windows 目录 bundle 使用标准入口 `Contents/x86_64-win/<bundle-name>.vst3` 加载；不递归猜测其他二进制，入口缺失时报错。其他平台继续将目录交给宿主。
- `POST /api/inference/vst/inspect` 返回 `parameters` 元数据（`type` 为 `boolean/string/number`）；`POST /api/inference/vst/process` 接受交错 f32le stereo `audio_base64` 及上述配置，返回处理后的同编码 PCM。
- 离线导出按轨道、总线、Master 链执行。启用 VST3 时，试听使用持续隔离混音会话，经有界缓冲返回 WebAudio；不是整曲预渲染，也不是低延迟声卡回调宿主。插件 GUI 嵌入不支持。
- `POST /api/inference/vst/editor`（推理服务`/vst/editor`）必须JSON。`{action:"open",path,parameters?,state_base64?}`以202返回`{id,path,state:"opening"}`；同服务只允许一个编辑器，忙时409。先恢复预设再应用参数；worker主线程调用原生show_editor，主服务不阻塞，opening不代表窗口可见性证明。
- `{action:"status",id}`返回opening/completed/failed；正常关闭completed同时返回`parameters,state_base64`，捕获失败不提交部分结果，不回退为参数JSON。`{action:"close",id}`异步关闭，5秒关闭超时后释放进程并标failed；未知会话404、非JSON415、错误请求400。状态请求维持30秒租约，服务退出释放子进程。
- `/vst/editor`仍为独立实例，工具页回读参数及预设；混音器显式确认应用到原轨道/总线/Master。参数覆盖预设值，换路径清除旧预设，离页关闭。另见下述实时实例入口；均不嵌入窗口，不保证插件未序列化数据。
 - `POST /api/project/effect/read`接受`{kind:"track"|"bus"|"master",id,index}`，返回原始`effect`及全工程`revision`；Master的id固定为`master`。缺失目标/索引404，非VST3效果400。
 - `PATCH /api/project/effect`接受相同目标及`expected_revision,parameters,state_base64?`。替换参数及显式提供的预设，缺失/null保留已有预设；路径、旁路、顺序和其他字段不变。复用`/mix/validate`校验，在校验前及提交写锁内检查版本；期间任何工程编辑409 `STALE_PROJECT`，不改工程或历史。成功保存历史并广播混音更新；预设变化要求重播，参数单独更新沿用现有规则。工程/携带包保留预设，插件本体和外部依赖不携带。
    
### 持续分块会话

- 同源入口 `/api/inference/vst/realtime/{open,process,update,close}`；均 POST JSON。`open` 接受 `sample_rate`、`block_frames`、`tracks` 和 `mixer`，返回 `session_id`及固定`latency_samples`。会话内部持有原生插件实例，正常分块处理不重置插件、不在块间排空尾音；启动主线程完成采样率初始化、reset及零输入预备，扣除预备已消耗的宿主起始省略预算。
- `process` 接受 `session_id`、从 0 连续递增的 `sequence` 及每条轨道的 `{id,audio_base64}`；音频为已做轨道声像的交错 f32le 双声道、恰好 `block_frames` 帧。返回同帧数 PCM、序号与 `processing_ms`。服务端统一执行轨道增益/效果、发送、总线和 Master，避免浏览器重复执行这些效果。
- 浏览器由专用模块 Web Worker 顺序发送处理请求并编解码；AudioWorklet 经可转移 MessageChannel 直接交付PCM，不经界面主线程转发。worker输入待处理队列最多5块，输出队列最多目标缓冲+3块；持续过载仍显式失败。音频时钟消息驱动预取/曲尾，不依赖requestAnimationFrame；主线程长期停顿仍可能影响尚未调度的未来音源，不保证任意后台/系统休眠连续性。
- `update` 接受完整轨道与混音配置，更新参数、旁路、增益、静音/独奏及现有轨道/总线发送的电平。发送电平不是拓扑；发送目标/数量、效果顺序、插件路径或其他路由拓扑变化要求停止后重播。工具页保存后的混音刷新不得重建浏览器原生试听图。`close` 释放 worker，重复关闭允许成功；关闭后的处理请求拒绝。
- `POST /api/inference/vst/realtime/editor`接受`{session_id,action:"open"|"status"|"close"}`；open另需`kind:"track"|"bus"|"master",id,index,path`（Master id省略或master）。index须非bool非负整数；非法目标/缺失path400、缺失目标404、非VST3效果400、路径不匹配409；重复打开409，不终止音频会话。未知/已关闭会话404，不回退独立编辑器。
- open以202返回`mode:"live_instance",session_id,state:"opening",kind,id,index,path`；窗口绑定现有链实例，在worker主线程运行，处理/更新在命令线程继续。status为存储状态读取并续租，不周期性复制参数；close请求异步关窗。窗口返回且参数/预设均捕获成功才发布completed及`parameters,state_base64`；失败仅返回failed/error，无部分提交。服务退出/会话关闭释放窗口和worker；5秒调用上限不变。
- 实时GUI改动仅作用于实际试听实例，已缓冲音频不变；不自动改工程。混音器关闭回读后显式确认，先停止试听再以原修订号提交；并发修改409拒绝，预设需重播。插件改变报告延迟时停止并要求重播；原生窗口与DSP并发仍受插件自身线程安全限制，崩溃/卡死通过隔离进程及超时释放，不保证任意插件编辑器兼容。
- 原生试听的短句预取、必需缓冲判定及音源调度均不按静音/独奏过滤；原生混音器决定可听性，以支持起播静音/非独奏轨在线恢复当前短句。仍使用6秒预取窗口、2个并发请求与64MiB缓存上限，可能增加起播合成等待；无VST试听沿用可听轨过滤。
- 原生在线声像在浏览器音源进入原生混音器前处理；更新须同步当前音源及调度计划的声像快照，使后续短句/剪辑使用最新值。服务端配置中的轨道pan保持0，避免重复声像处理。
- 插件失败、处理超时或排队溢出显式停止试听，不静默退回干声。暂时欠载输出静音并计数，积累目标块数后按序恢复，不丢弃已排队PCM；不把静音段计入已播放音频帧。暂停、停止、跳转及内容版本变化取消旧会话和流worker，迟到结果不得恢复声音。界面缓冲毫秒数不是声卡端到端测量。
- 持续会话上限为4个、32轨、32效果、每块1–8192帧、8–192kHz；空闲30秒回收。浏览器使用2048帧与4块目标缓冲，实际欠载单独计数。补偿延迟最长路由最多10秒，立体声f32延迟线总内存最多64MiB，超限拒绝启动。
- 试听尾音预算按已启用效果的显式 `tail_seconds` 串联相加；轨道输出/发送、总线输出/发送到 Master 的并行路径取最长，未连接总线和旁路效果不贡献预算。预算以工程音源结束为起点，处理更新成功后重算，支持播放中延长或缩短；这不是自动检测插件完整衰减，也不增加内建效果缺省尾音配置。
- 持续PDC在每个汇合点为较短路径分配跨块环形延迟线；轨道输出/发送、总线输出/发送及Master串联均参与，未连接总线不延长可听路径。插件报告延迟在旁路时亦保留，插件和对应干声延迟持续处理，保证启用切换不改变时间原点。返回PCM省略的起始缓存以对应零前缀补回，不再用首块短缺猜测报告延迟；超出报告预算的缓存拒绝处理。
- 参数更新或处理后报告延迟变化，终止会话并返回409 `RESTART_REQUIRED`，须停止重播；不热改延迟线或静默错位。浏览器`plugin_latency_ms`与网络`latency_ms`分开显示；播放头以`max(0,heard_frames-latency_samples)`推进，使自然结束时补偿尾部已排空。固定预算包含起播旁路插件，不代表低延迟声卡宿主；误报/未报告延迟不保证纠正。


## 12. 依赖清单入口

Python 使用根目录 `requirements*.txt`，用 `-r` 组合配置；`requirements.txt` 为 CPU 基础，`requirements-build.txt` 为冻结打包，其他按功能独立安装。`requirements-native.txt` 是旧版 CPU DiffSinger 独立环境，不能与现代 `requirements-gpu.txt` 混装。CPU `onnxruntime` 与 `onnxruntime-gpu` 不得共存。清单为版本约束，不是全平台哈希锁。

`requirements-system.txt` 是非 pip 工具及外部资源清单；Rust/Cargo 和前端/npm 仍由原生 manifest/lock 管理。声库、模型、插件和凭据不由 pip 自动安装或重新授权。

冻结构建配置同时包含 `requirements-vst.txt` 的 Pedalboard 宿主依赖，并收集其原生运行文件；不因此包含任何 VST3 插件。安装模式可以通过 `SVS_PLUGIN_DIR` 指定独立插件根目录。

## 13. 工程与外部媒体携带包

`POST /api/interchange` 新增 `export_bundle` 与 `import_bundle`，对应 `.svsbundle` ZIP 容器，不是云同步。

- 导出：`{"action":"export_bundle","path":"C:/Music/project.svsbundle","project_base":"C:/Music/source"}`。服务端默认使用当前工程快照；相对音频路径必须显式提供 `project_base`。目标不得已存在。
- 导入：`{"action":"import_bundle","path":"C:/Music/project.svsbundle","destination":"C:/Music/restored"}`。恢复目录必须不存在；验证通过才发布目录并替换当前工程，后续默认保存指向恢复后的 `project.json`，不沿用旧工程保存位置。
- 包内 `manifest.json` 声明 `format=svs-project-bundle`、`version=1`，`payloads` 映射每个成员的字节数与 SHA-256。工程为 `project.json`，媒体以 `media/<SHA-256>.<原扩展名>` 去重存放。恢复后音频路径改为新目录的绝对路径。
- 拒绝路径穿越、符号链接、重复或清单外成员、缺失媒体、长度/哈希不符和超限归档。归档上限2GiB、单成员1GiB、总展开4GiB、最多10000成员；媒体校验和恢复按块处理，导出使用不压缩ZIP。
- 保留工程音符、曲线、混音和外部资源引用，不打包声库、模型、插件或服务凭据；依赖这些资源的工程仍须在目标机器另行安装并核验授权。SHA-256 是完整性校验，不是发布者身份签名。


## 14. 手动 WebDAV 工程同步（Windows）

- `POST /api/inference/sync`（推理服务`/sync`）接受JSON：`status`、`configure`、`list`、`disconnect`。配置为`{action:"configure",url,username,password}`，URL必须是已存在的WebDAV集合；仅HTTPS，字面回环主机允许HTTP用于本地服务。拒绝URL内凭据、查询/片段及路径穿越；不跟随重定向，不关闭TLS证书验证。
- 凭据使用Windows当前用户DPAPI加密，与集合/用户名/修订记录保存在`%APPDATA%/SVS Studio/project-sync/profile.json`；测试可用`SVS_SYNC_STATE_DIR`隔离。非Windows不提供明文回退。API不返回密码，项目包不携带凭据；断开删除保存的配置与凭据，保留备份。
- `list`通过Depth1 PROPFIND只列集合直接子级`.svsbundle`，返回`items:[{name,etag,size,modified}]`；XML的etag仅显示，不直接作为覆盖依据。远程文件名为单一安全basename，允许中文，不接受目录/控制字符/URL分隔符。
- 列表按`DAV:`命名空间和每个`propstat/status`解析，只消费状态200的属性；失败属性与其他命名空间同名属性不能覆盖有效ETag/大小/修改时间。集合识别也只读取成功的`DAV:resourcetype`。
- `POST /api/interchange`的`sync_push`为`{action,remote_name,project_base?}`。服务端捕获当前工程快照并打包媒体；创建使用`If-None-Match:*`，已知修订更新使用`If-Match:<strong ETag>`。无基础版本、远端变更或条件拒绝时另存唯一冲突文件，原文件不被盲目覆盖。上传后条件GET读回比对SHA-256，验证成功才保存基础ETag；不自动删除远端。
- `sync_pull`为`{action,remote_name,destination,project_base?}`。下载必须有强GET ETag；先将当前工程及媒体备份至本机同步状态目录`backups/`，再按§13验证恢复到尚不存在的新目录。成功返回`project,project_path,backup_path,etag`并设置新默认保存位置；凭据/声库/插件仍不携带。
- 下载期间当前工程版本变化时返回409`STALE_PROJECT`，不替换当前工程；已校验下载目录及备份仍保留，响应返回路径。新同步请求必须为application/json，防止简单跨站表单请求；网络/TLS失败、401认证拒绝与422本地验证错误明确返回，不报告假成功。
- 此功能是手动整包上传/恢复与冲突保留，不是自动双向合并。真实回环WebDAV协议验收不代表Nextcloud公网部署、跨机器恢复、特定服务端条件语义或服务器备份运维已验收。

## 15. 官方声库原包安装

- 同源`/api/inference/voices`代理推理服务`/voices`。`GET /voices/catalog`返回已审核的固定目录`items`和`install_root`，条目含`id,name,version,bytes,sha256,source_url,terms_url,notice,downloaded_bytes`；当前仅绮萱2.7.0官方GitHub原包，不接受调用者URL或凭据。`downloaded_bytes`为磁盘缓存长度，应用重启后仍可查询。
- `POST /voices/install`要求JSON`{catalog_id:"qixuan-2.7.0",accepted_terms:true}`，成功202返回`{job}`；非JSON415、未接受条款/未知目录422、任务运行中/目标已存在409。任务状态`downloading → extracting → registering → completed`，异常进入`failed`；`GET /voices/install`返回进程内最近任务`{job:null|{id,catalog_id,state,downloaded_bytes,total_bytes,resumed_bytes?,destination?,voice?,error?}}`。重启不自动执行下载，用户重新接受条款并点击继续后从持久断点恢复；UI重新挂载读取当前任务，完成后刷新登记列表。
- 原包缓存为安装根`.downloads/<catalog_id>-<sha256>.part`，中断或退出保留，已完整缓存重新校验后可直接安装。续传每次从原始官方URL取得新重定向，以`Range: bytes=<缓存长度>-`请求；206必须精确匹配Content-Range起点/末端/总长及可选Content-Length，不符拒绝拼接且保留断点；返回200则在校验响应头后覆盖重下。仅接受identity编码，累计长度与完整SHA-256匹配后才解包；完整文件哈希不符则删除损坏缓存，超长缓存重置。不保存临时签名URL或凭据。
- 仅HTTPS官方GitHub及其审核过的资源域重定向，保持TLS校验；失败不使用镜像、不跳过校验，错误不暴露签名重定向URL。解包暂存目录在失败时清理，已创建的安装目录回滚；清理完成后才登记并发布成功。下载缓存独立于解包暂存目录，成功后仍可用于免重复下载的重装，已有安装仍拒绝覆盖。
- 解包前校验全部路径，拒绝绝对路径、穿越、反斜杠、Windows保留名、链接/特殊文件、加密、重复及文件/目录冲突；最多10000成员、单成员1GiB、总展开4GiB。保留原包全部文件及条款，不修改声库配置；目标通过原子创建预留，不覆盖。手动登记/删除与安装登记共用注册表事务锁。
- Windows默认`%LOCALAPPDATA%/SVS Studio/voicebanks/<catalog_id>/content/`，非Windows为XDG数据目录；`SVS_VOICE_INSTALL_DIR`可隔离。独立声码器、依赖、模型执行与商用授权确认均不包含；原包安装不是R2/Supabase账号、签名鉴权或自建托管服务。
## 16. 已有独立声码器绑定

- `PATCH /api/inference/voices/<voice_id>/vocoder`（推理服务`/voices/<voice_id>/vocoder`）接受JSON`{path:"本机包含vocoder.yaml的目录",acknowledged_license:true}`，绑定到已登记声库；`{path:null}`清除绑定。只保存目录引用，不下载、复制模型或修改声库原包。未知声库404，未确认独立许可/路径或兼容性错误400，失败不写注册表。
- 外部目录须含vocoder.yaml及其明确引用的模型文件，模型引用不得逃出选择目录；对照声学dsconfig.yaml校验vocoder名称和sample_rate/hop_size/win_size/fft_size/num_mel_bins/mel_fmin/mel_fmax/mel_base/mel_scale。不以目录中任意ONNX存在代替被引用模型完整。
- 声库描述新增`vocoder_path`与`vocoder:{source:"external"|"bundled"|"missing",path,compatible,missing_files,issues}`；外部绑定优先于随声库提供的dsvocoder，清除后恢复随声库目录检查。`inference_ready`仅表示已配置运行时文件及静态前置条件，界面不得把它标为真实合成通过。
- 绑定为本机注册表设置，不随工程或媒体包携带；重新登记同一路径须保留绑定。模型加载及合成使用绑定的真实组件，不修改上游包或声库；原生预测器/PCM缓存身份包含所选声码器和文件修订，不能复用旧绑定音频。绑定/清除前界面停止试听，之后须重新合成/播放。
- 仍需在启动时显式配置兼容外部`SVS_GPU_PYTHON`与`SVS_ABC2VOCALS_DIR`；配置变化后重启。许可确认只表示用户已阅读独立声码器条款，不授予商用权、不改变声库/声码器许可，不自动安装运行时或下载权重。

## 17. 本机原生声卡输出

- `POST /api/audio/output/open`接受JSON`{sample_rate,block_frames,target_blocks,device_id?:string|null}`；省略/null使用默认设备，显式ID只匹配所选设备，不可用/同名歧义503，绝不回退默认。采样率8000..192000，块1..8192帧，目标1..128块，容量`block_frames*(target_blocks+3)`不超过1048576帧。所选设备须双声道且默认采样率与请求匹配，支持f32/i16/u16；设备/格式不可用503`OUTPUT_UNAVAILABLE`，配置越界400，活动流409`OUTPUT_BUSY`。返回`{id,sample_rate,channels:2,device,device_id,capacity_frames,target_frames}`。
- `GET /api/audio/output/devices`枚举本机默认CPAL host输出，返回`{devices:[{id,name,is_default,selectable,sample_rate?,channels?,reason?}]}`。ID为精确设备名SHA-256，不用枚举序号；同名设备禁用显式选择并报歧义。该ID不是系统端点GUID，设备重命名后须刷新重选；设备名读取失败项无法安全标识，不列入。
- `POST /api/audio/output/{id}/pcm`提交恰好一块交错双声道f32le二进制，头`x-audio-sequence`从0递增。非有限/长度非法400`INVALID_PCM`，错序409`SEQUENCE_MISMATCH`，队列满409`OUTPUT_FULL`；拒绝不写入、不递增序号，客户端稍后原序重试。整块原子发布，不观察半帧。
- `GET /api/audio/output/{id}`返回`{id,device,device_id,sample_rate,queued_frames,played_frames,underruns,started,error?}`。played_frames仅计实际消费内容，欠载填零不推进；达到target_frames后启动，欠载后重新预缓冲。回调只消费预分配原子环形队列，无分配、锁、网络或插件处理；设备错误原子标记并由状态暴露。
- `DELETE /api/audio/output/{id}`幂等关闭并等待设备owner线程释放，失败503。状态/PCM维持心跳，无心跳约5秒释放，下次open清除过期项；未知ID404，不影响新流。精确Tauri来源允许`x-audio-sequence`头。
- 走带输出为会话设置，不写工程。原生模式经隔离混音进程后转发本机声卡；AudioWorklet仅捕获源，不同时浏览器播放。时钟来自设备消费帧并扣插件补偿；停止/暂停/定位关闭旧流。仍有WebAudio源调度、HTTP/VST块缓冲和系统设备缓冲，不声称端到端低延迟、ASIO/独占模式或远程声卡支持。
- 停止时可刷新并选择输出设备，播放期间禁用选择/刷新；显式选择缺失后保留原ID并提示不可用，不静默改为默认。设备与采样率为会话状态，不写工程；切换到不同默认采样率时重建AudioContext及音频图/源缓存，按新设备采样率生成VST块，不把44.1kHz数据作为48kHz播放。不支持热切换、ASIO或独占模式。
- 前端会话设置`audition:{block_frames,target_blocks}`允许分块512/1024/2048/4096帧及预缓冲2/4块，默认2048/4；播放期间禁用设置，启动时快照到当前会话，非法组合在创建混音/设备流前拒绝。不写工程，不静默自动改变分块以掩盖过载。所选分块一致用于混音进程、AudioWorklet捕获和设备PCM提交；目标块数用于浏览器输出或原生设备预缓冲。分块须为128帧量子的整数倍；显示预算`block_frames*target_blocks/sample_rate`不是端到端延迟，小分块可能欠载/过载。

