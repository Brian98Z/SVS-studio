# SVS Studio

**独立开发的本地优先歌声合成与编曲工作站**，面向 MIDI/歌词编辑、歌声合成、音乐素材处理和工程导出。

[English](README.md) · 中文

> 当前版本：0.1.0。

## 功能概览

- Vue 桌面界面、Rust 本地服务与 CLI、Python 推理服务及 stdio MCP 服务。
- 歌曲工程、多个轨道、音符与歌词、速度/拍号、表现力曲线编辑；以 480 PPQ 工程时间格和 24 kHz 推理音频为基础。
- 基于 DSP 的本地合成路径；可选接入 DiffSinger 原生推理及通用 ONNX 管道。实际模型、声库和算子支持取决于用户自行配置的外部资源。
- 乐句 PCM 试听、WebAudio 混音、效果与总线路由，并导出 24 kHz 双声道 f32 WAV。
- MIDI、MusicXML、LRC、UfData 等互通路径，以及音频素材、分轨和 FCPXML/AAF 等媒体工具；具体格式和功能以当前实现为准。
- 可选 Basic Pitch 人声转 MIDI、Demucs 人声分离，以及通过已配置聊天服务调用的主题作曲/伴奏功能。

本地 GPU 路径已在一个 Windows x64 环境配合外部 Python/声库验收：DiffSinger 原生 ONNX 会话使用 CUDA；Basic Pitch 的 440 Hz 测试音转为 MIDI 69；Demucs GPU 路径产生四路分轨。这些结果不意味着模型、声库、CUDA 运行时或音乐资产包含在仓库，也不保证其他机器或声库可直接运行。AI 主题作曲/伴奏走配置的外部聊天服务，不是本地 GPU 大语言模型。

## 架构

```text
Vue 桌面应用  ── HTTP/WebSocket :8761 ── Rust svs-server
                                              │
                 CLI / stdio MCP ─────────────┤
                                              └── HTTP :8760 ── Python inference-service
                                                                    ├── DSP
                                                                    ├── ONNX / DiffSinger adapters
                                                                    └── optional music-AI routes
```

跨组件数据和行为约定见 [`docs/CONTRACT.md`](docs/CONTRACT.md)，开发方向见 [`docs/dev_roadmap.md`](docs/dev_roadmap.md)。

## 前置条件

源码开发需要 Rust stable 工具链（Windows 使用 MSVC 工具链）、Node.js/npm，以及 Python 3.10+ 与 pip。桌面窗口需要 WebView2。GPU 推理还需要与目标硬件、驱动和依赖版本匹配的 CUDA 运行时，以及外部模型和授权声库。
请在仓库根目录使用统一的 Python requirements 配置：`requirements.txt` 是 CPU 桌面运行时聚合配置；`requirements-core.txt` 是共用运行时；`requirements-build.txt` 增加 PyInstaller；`requirements-mcp.txt` 用于 stdio MCP server。可选配置包括 `requirements-g2p.txt`（多语言 G2P）、`requirements-media.txt`（AAF 互操作）、`requirements-vst.txt`（离线 VST 处理）、`requirements-music-ai.txt`（Basic Pitch/Demucs/PyTorch）、`requirements-gpu.txt`（GPU 推理）、`requirements-native.txt`（旧版外部 DiffSinger 集成依赖）和 `requirements-dev.txt`（开发/测试依赖）。按需安装；VST 不属于默认运行时。这些文件提供版本约束，不是覆盖所有平台的哈希锁定文件。Rust 与 JavaScript 依赖仍以 Cargo/npm 清单和锁文件为准。`requirements-system.txt` 仅盘点系统工具；pip 无法安装 Rust、npm、FFmpeg/ffprobe、模型或声库。
CPU 与 GPU 版 ONNX Runtime 不得共存于同一环境。音乐AI GPU用途请在独立新venv安装`requirements-gpu.txt`，不要先装包含CPU ORT的`requirements.txt`。原生DiffSinger CUDA合成使用下述独立`requirements-native-gpu.txt`。旧`requirements-native.txt`仍为CPU配置：Python3.11下上游0.3.8要求旧CPU ORT/Torch。不要合并这些配置或修改已安装包元数据强行兼容。

已登记的DiffSinger声库可在“工具→声库”绑定本机已有声码器：填写包含`vocoder.yaml`的目录，独立阅读其许可并确认后绑定。名称、声学/mel参数须匹配，配置引用的ONNX须位于该目录且存在；清除绑定仅删除引用，不改原声库或模型。启动前配置兼容外部运行时，绑定后重新合成/播放；绑定只保存在本机注册表，不随工程携带。“前置文件齐备”不是合成通过或商用授权证明，不自动下载权重/运行时。
当前Windows安装器已隔离安装验收：随包冻结运行时支持绑定/清除，另配外部GPU Python及声码器可真实合成。另已按下述原生CUDA配置在两个新Windows环境安装全部依赖、通过pip check，并在源码HTTP服务实际加载8个CUDA会话输出24kHz歌声；安装版与源码服务为分别观察，不代表捆绑GPU环境、其他机器保证、商用许可或任意声库兼容。
`requirements-music-ai.txt` 包含 Basic Pitch 与 Demucs/PyTorch。Python 3.11 下，Basic Pitch 0.4 元数据要求 TensorFlow >=2.4.1,<2.15.1，即使项目实际使用其 ONNX 路径；常规依赖解析也会安装 TensorFlow。
相关媒体导入/导出功能需要系统 PATH 中有 FFmpeg/ffprobe。

## 从源码构建与运行

在仓库根目录使用 PowerShell 或兼容 shell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cargo build --release -p svs-server -p svs-cli
cd apps/desktop
npm ci
npx vite build
cd ../..
```

从仓库根目录启动服务。它在 `http://127.0.0.1:8761` 提供已构建的桌面前端，并通常会将推理服务作为子进程启动：

```sh
./target/release/svs-server
```

Windows 可运行 `target/release/svs-server.exe`。`--no-inference` 只跳过推理子进程启动；合成请求仍可使用已在 8760 端口运行的独立推理服务。

```sh
./target/release/svs-server --no-inference
```

推理服务子进程使用上述 CPU 桌面依赖。若需单独启动推理服务，请激活相应环境并在推理服务包目录运行：

```sh
cd packages/inference-service
python -m inference_service.service_runner --port 8760
```

可选功能配置也都从仓库根目录按需安装。

### 可选外部 GPU 歌声运行时

GPU 歌声推理不是干净克隆后即可独立使用的功能。需要自行提供兼容的外部 Python 环境、DiffSinger 模型文件、声库，以及所需的 `abc2diffsinger` 集成/插件和 `diffsinger_utau` 运行依赖。通过 `SVS_ABC2VOCALS_DIR` 指定外部插件目录；启动 server 前，将 `SVS_GPU_PYTHON` 设为 Python 可执行文件路径，将 `SVS_VOICEBANK` 设为经授权的声库目录，并设置 `SVS_INFERENCE_DEVICE=cuda`。源码中的插件默认路径属于开发机器，其他机器应明确覆盖。显式 CUDA 初始化错误会报告失败，不会静默回退 CPU；CPU DSP 是另一条独立路径。未经资源权利人许可及适用许可授权，不得分发模型或声库。

Windows x64先安装Python3.11与`uv`，从仓库根目录执行（目标目录必须不存在）：

```powershell
.\scripts\setup_native_cuda.ps1 -EnvironmentDirectory C:\SVS\native-cuda -PythonExecutable C:\Python311\python.exe
$env:SVS_GPU_PYTHON = 'C:\SVS\native-cuda\Scripts\python.exe'
```

脚本由SHA-256固定的MIT上游源码包构建`diffsinger-utau==0.3.8+svscuda1`，保留运行时Python字节及许可，仅明确修改构建期CUDA依赖声明；正常pip解析全部依赖并执行`pip check`，不使用`--no-deps`或重写已安装元数据。wheel及来源清单位于`build/native-cuda/wheels`，需要重新构建时先移除该生成目录。配置选择Torch2.11.0+cu130、ORT GPU1.31.0、NumPy1.26.4、SciPy1.13.1；不含BasicPitch/Demucs，不安装外部ABC集成、声库、声码器、驱动或模型。下载可能数GB；版本约束不是完整传递哈希锁，不保证其他机器可用。

Windows/Python3.11的`requirements-gpu.txt`已在新虚拟环境完整安装并通过`pip check`，实际完成RTX5070Ti的CUDA Basic Pitch与Demucs HTTP请求；也安装了Basic Pitch元数据声明的TensorFlow依赖。该现代音乐AI配置不包含全部旧版DiffSinger原生集成依赖，原生声库环境仍须单独配置。验收复用本机已有官方模型缓存，不代表仓库或安装器捆绑模型。

## CLI 与 MCP

`svs-cli` 向本机 `127.0.0.1:8761` 的 server 发送请求，不是独立工程编辑器。不带参数运行会打印命令摘要并以错误状态退出；以下示例对应 CLI 命令解析器：

```sh
./target/release/svs-cli new
./target/release/svs-cli track add sing Lead
./target/release/svs-cli note add <track-id> --tick 0 --dur 480 --key 60 --lyric Hello
./target/release/svs-cli render output.wav
```

Windows 使用 `target/release/svs-cli.exe`。MCP server 是面向兼容客户端的独立 stdio 进程；从仓库根目录安装 `requirements-mcp.txt` 并运行：

```sh
python -m pip install -r requirements-mcp.txt
python apps/mcp-server/server.py
```

将 MCP 客户端配置为以上启动命令及对应工作目录。CLI/MCP 仅支持各自接口已实现的操作；当前能力请参考其源码和契约。

## Windows 桌面打包

打包入口为 `scripts/package_desktop.ps1`。构建需上述 Rust/MSVC 与 Node 工具链，以及 Tauri 的 Windows 前置条件。Windows 系统可能需要 WebView2 Runtime。打包不会包含第三方声库、合成模型、CUDA 运行时，也不保证附带可用的 GPU Python 环境；使用原生 GPU 合成须另行配置兼容的外部环境和已授权资源。

打包使用独立环境 `apps/desktop/src-tauri/.venv-build`：通过 `python -m venv apps/desktop/src-tauri/.venv-build` 创建，激活后从仓库根目录安装 `requirements-build.txt`。运行打包脚本前先构建前端。应用与构建停止后，可删除自动生成的 `src-tauri/runtime/`、`src-tauri/gen/`、Python/pytest 缓存与 PCM 缓存，使用时会重新生成；须保留打包环境、锁文件、最终安装器、用户工程、声库及验收证据。清除 PCM 缓存后首次合成将重新推理。当前脚本实际使用 `.venv-build` 冻结运行时，不使用可选参数 `-PythonExecutable` 指定的环境。

冻结打包配置包含 Pedalboard VST3 宿主及其原生运行依赖，不包含第三方插件二进制。安装后的应用可通过 `SVS_PLUGIN_DIR` 指定可信插件目录；工程携带包只保留插件引用，不携带插件。

Pedalboard 0.9.26 采用 GPLv3。应用原创源码的 Apache-2.0 许可不会免除包含该依赖的二进制分发义务。公开分发安装器前须单独核对 GPL 合规、许可声明和适用的对应源码；本地安装验收不代表该发布门槛已解除。

## 验证范围

项目包含 Rust/Python 契约及组件检查；路线图记录最近观察到的集成状态。曾在 Windows x64 上使用外部 Python 环境和声库，验证原生 DiffSinger CUDA 会话、CUDA Basic Pitch 转录及 Demucs 分离。这些机器特定结果仅说明该配置下的路径，不保证干净克隆或任意模型/声库可用。

## 已知限制

当前优先级与解除条件统一见[剩余阻塞清单](docs/dev_roadmap.md#剩余阻塞清单2026-10-20)B01—B10。先修已复现的桌面进程退出故障，再处理音频稳定性／延迟；模型及公网服务须先具备对应资产、授权或账号。ASIO／独占、播放中设备热切换及VST内嵌界面仍未实现；物理拔插、真实同名设备仍未验收。公开分发须通过许可与干净机器验收，不能由本地安装通过代替。本次文档更新不解除门槛、不重打安装器。

- VITS 适配器尚未实现；多说话人和情绪控制未针对兼容模型完成通用验证。
- 同模型DiffSinger Blend将复制的阶段预测器绑定到混合speaker reader，原缓存不变。已用合成向量验证真实声学输入准备；这不等于真实多说话人音频验收，也不代表取得商用声库许可。
- 工具→声库在显式接受条款后可下载官方绮萱2.7.0原包。中断或退出后保留断点，重启后再次接受条款并点击“继续下载并安装”；仅在HTTP续传范围匹配时追加，服务器忽略Range则安全重下。完整长度/SHA-256匹配后完整安全解包并登记，不覆盖已有安装。安装根`.downloads`保留原包缓存以便复用。不转存模型、不提供账号分发服务，不自动安装独立许可声码器及推理环境；安装不代表可推理或取得所有商用授权。默认安装至本地用户数据目录，可用`SVS_VOICE_INSTALL_DIR`覆盖。
- 显式要求 CUDA 时，设备/运行时不可用会报错；音频解码、部分后处理与混音仍可能运行于 CPU。
- VST3 支持轨道、总线与主总线的隔离离线处理，以及持续分块、有缓冲的试听；不是预渲染整曲。播放时可调参数、旁路与混音电平；更换插件顺序/路径或路由须停止后重播。走带栏显示缓冲与欠载，不是低延迟声卡回调宿主。可信插件放入 `plugins/`，工程保存相对路径；中文显示字典 `apps/desktop/src/audio/effect-labels.zh-CN.json` 不改变原始参数键和值。TDR Nova 2.2.3 与 WET Reverb 1.3.0 已完成离线实效验收，WET尾音覆盖24/44.1/48kHz；WET重复及分块结果不逐样本相等，不保证任意插件兼容。不支持插件 GUI 嵌入；插件不随应用重新分发。
- 停止时选择“浏览器输出／原生声卡输出”，原生模式可刷新列表并选择设备或系统默认；按所选设备默认采样率播放，跨采样率切换重建源AudioContext。显式设备缺失/同名歧义报错，不回退默认；设备改名后须刷新重选。选择只保留本次会话，不写工程。CPAL回调消费有界PCM队列，浏览器该路静音；设备队列/欠载独立显示，时钟跟随消费帧。停止释放设备，失联约5秒过期。仍有WebAudio源调度、隔离VST、HTTP和设备缓冲，不支持ASIO/独占/播放中热切换，不代表已测得DAW级低延迟；不要用于远程server。
- 试听按路由计算尾音：已启用效果的显式 `tail_seconds` 串联相加，并行路径到 Master 取最长；旁路和未连接效果不延长预算。播放中修改尾音在服务端更新成功后重算停止点；不自动检测插件完整衰减，也不是插件延迟补偿。
- 停止时可选512/1024/2048/4096帧分块、2或4块预缓冲，默认仍2048/4。仅本次会话有效，作用于原生VST处理/声卡路线，不影响无VST普通浏览器播放。小分块增加HTTP请求压力，可能欠载或在持续过载时明确停止；不稳时增大分块/预缓冲。显示的分块预算不包含设备驱动、捕获和网络开销，不是端到端延迟实测。
- 当前Windows验收发现退出限制：关闭窗口后后端服务已停止，但桌面主进程15秒后仍驻留；本安装器不宣称正常进程退出已通过。
- 离线VST渲染去除报告样本延迟并排空末尾，扣除宿主已省略的起始缓存。持续试听按固定会话预算对齐轨道输出/发送、嵌套总线及Master；旁路保留预算并持续运行插件及干声延迟，改变插件延迟的参数会停止会话并要求重播。走带分别显示插件补偿与网络缓冲，可听时钟扣除起始补偿以保留曲尾。误报或未报告延迟不能自动纠正。两项补偿已进入当前本地验收的Windows安装器。
- 工具→VST3保留隔离独立编辑器；混音器另可打开当前持续试听中轨道/总线/Master的实际插件实例窗口，修改作用于后续缓冲音频，不自动修改工程。关闭后显式确认会停止试听，按打开时的工程修订号提交参数与原生预设；期间工程修改拒绝覆盖。预设随工程/携带包保存，先恢复预设再叠加参数；预设上限4MiB，校验格式/插件class，外部采样/文件、未序列化数据和运行中DSP历史不携带。当前Windows安装器已隔离安装并用随包冻结运行时完成Nova原生鼠标−12→−6dB、同会话PCM、界面确认及保存重开验收，无外部Python配置。非内嵌，不保证任意插件/版本或低延迟声卡回调；安装器未签名、未公开发布，不代表第三方再分发合规已完成。
- 新增WET Reverb 1.3.0及Dragonfly 3.2.10早期反射/Hall/Plate/Room的预设＋参数、24/44.1/48kHz轨道/总线/Master离线与持续处理验收。预设不能单独代替参数：WET预设未恢复所选模式，Dragonfly写参数后未处理时原生状态仍可能是旧值，须共同保存。当前安装器已修复混音标准化丢弃参数覆盖的问题；上版预设安装验收未覆盖此项。Dragonfly早期反射编辑器/工程回读及安装版冻结运行时修复已实测，其他原生编辑器未验收。
- 原生试听中可实时调节已有轨道/总线发送的电平，混音器保存后保持原生会话；增加、删除发送或更换发送目标仍须停止重播。
- 原生试听也会预取静音及非独奏轨的短句，解除静音或取消其他轨独奏后可恢复当前短句，无须重播；因此起播合成等待可能增加，仍受原缓存上限约束。静音/独奏变化在已缓冲输出播放后生效。
- 原生试听在线声像同时作用于当前音源及后续短句/音频剪辑，不在下一音源起播时回到旧值；仍需等待已缓冲输出。
- 原生PCM编解码与网络请求使用专用Web Worker，通过MessageChannel直连AudioWorklet，不随界面主线程暂停；音频时钟在无动画帧时仍驱动调度。队列有界，欠载后按序补缓冲，持续过载明确停止；不保证系统休眠或无限主线程停顿期间连续播放。
- 工具→工程同步支持Windows手动WebDAV整包上传与恢复。配置已存在的HTTPS集合URL及应用密码，凭据以Windows当前用户DPAPI加密。ETag条件更新防止盲目覆盖，冲突另存副本；恢复要求新目录、先备份当前工程与媒体，下载期间工程被编辑则拒绝替换。已验证真实认证回环WebDAV；未验收公网Nextcloud部署及跨机器云恢复，不是自动双向合并。AI主题作曲与伴奏仍使用外部聊天服务。
- WebDAV列表按属性状态及XML命名空间解析，失败属性与同名扩展字段不会覆盖成功的DAV元数据；此兼容性修复不代表公网服务或跨机器验收完成。
- 工具页“乐谱导入/导出”可选择“SVS 工程包（含音频）”：将工程和外部音频打成带 SHA-256 清单的 `.svsbundle`，恢复到尚不存在的新目录；相对源音频路径需填写工程原目录。不会携带声库、模型、插件或凭据，也不会覆盖现有输出；这不是云同步。
- 同步相对媒体路径前须填写工程原目录。本机恢复前备份位于`%APPDATA%/SVS Studio/project-sync/backups/`；断开只删除配置与凭据，不删除备份。声库、模型、插件不上传；安全凭据持久化目前仅支持Windows，其他平台拒绝而非明文保存。
- 本机 GPU 验收不代表仓库源码包含 Python 环境、CUDA、声库、模型或用户歌曲。

## 许可与第三方资源

SVS Studio 原创源码和文档采用 [Apache License 2.0](LICENSE)（SPDX：`Apache-2.0`）。Copyright 2026 Brian98Z and SVS Studio contributors。第三方依赖、模型、插件、声库及外部服务仍遵循各自许可与条款；本项目许可不改变这些资源的许可，也不授予其资产使用权。使用或再分发前请核验对应条款。
