# SVS Studio

**独立开发的本地优先歌声合成与编曲工作站**，面向 MIDI/歌词编辑、歌声合成、音乐素材处理和工程导出。

[English](README.md) · 中文

> 当前版本：0.1.0。

**发布状态：**当前尚无已解除公开分发门槛的安装包。维护者审核草稿不是公开下载，见[代码审计与发布门禁](docs/release-audit.md)；GitHub自动生成的Source code压缩包是源码，不是安装程序。

## 功能概览

- Vue 桌面界面、Rust 本地服务与 CLI、Python 推理服务及 stdio MCP 服务。
- 歌曲工程、多个轨道、音符与歌词、速度/拍号、表现力曲线编辑；以 480 PPQ 工程时间格和 24 kHz 推理音频为基础。
- 基于 DSP 的本地合成路径；可选接入 DiffSinger 原生推理及通用 ONNX 管道。实际模型、声库和算子支持取决于用户自行配置的外部资源。
- 乐句 PCM 试听、WebAudio 混音、效果与总线路由，并导出 24 kHz 双声道 f32 WAV。
- VST持续试听采用二进制PCM、隔离插件进程及可选原生输出。Windows支持WASAPI共享／独占和同采样率播放中换设备，其他平台保留CPAL共享输出；内建EQ／混响跨块保留状态。默认2048帧／4块不变，小分块仍可能欠载；显示预算不是实测端到端延迟，ASIO未实现。
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

打包与发布准备工具统一默认使用`apps/desktop/src-tauri/.venv-release-lock/Scripts/python.exe`；可用`-BuildPython <路径>`指定其他冻结环境，但仍须通过原始哈希锁wheel校验。用CPython3.11创建默认环境（`py -3.11 -m venv apps/desktop/src-tauri/.venv-release-lock`），再以`--require-hashes`安装`scripts/release-windows-py311.lock`。打包脚本自行构建前端。应用／构建停止后可清除`src-tauri/runtime/`、`src-tauri/gen/`、Cargo target、PyInstaller工作目录及Python／pytest缓存；清理后先运行完整打包入口重建runtime，不能直接调用Tauri打包。保留锁定构建环境、原始wheel、许可／源码输入、锁文件、当前安装包及紧邻回退包、用户工程、声库和验收证据；清除PCM缓存后首次合成会重新推理。

运行`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package_desktop.ps1 -PrepareOnly`检查前置，实际打包时去掉`-PrepareOnly`。`-Wheelhouse <目录>`可选，默认仓库内缓存，从其他工作目录调用亦可。许可收集保留供应商Notice及无固定扩展名的许可文本，排除Python辅助代码和二进制。覆盖安装只迁移删除旧包误带的6个许可辅助文件，不整目录清空runtime或用户数据。

冻结打包配置包含 Pedalboard VST3 宿主及其原生运行依赖，不包含第三方插件二进制。安装后的应用可通过 `SVS_PLUGIN_DIR` 指定可信插件目录；工程携带包只保留插件引用，不携带插件。

所有VST引用（含工程恢复出的绝对路径）必须解析在本机配置的可信插件目录内；UNC／设备路径和解析后越界路径在加载前拒绝，导入工程不授权目录外原生代码。DiffSinger模型引用同样须在检查／创建预测器前确认是声库内部本地路径；独立外部声码器仍需本机显式选择。

Pedalboard 0.9.26 采用 GPLv3。应用原创源码的 Apache-2.0 许可不会免除包含该依赖的二进制分发义务。公开分发安装器前须单独核对 GPL 合规、许可声明和适用的对应源码；本地安装验收不代表该发布门槛已解除。

Windows CPython3.11冻结包使用`scripts/release-windows-py311.lock`锁定47项版本／wheel SHA-256，在干净`.venv-release-lock`以`pip install --require-hashes -r scripts/release-windows-py311.lock`安装。打包核对已安装版本并携带许可清单；`release/python`保留Pedalboard tag及五个精确gitlink源码归档与哈希，属于对应源码候选，不证明与PyPI wheel构建来源完全一致或GPL审核完成。其他机器／平台尚未验收。

源码仓库有意忽略`release/`，打包所需材料及逐项哈希见[docs/release-inputs.json](docs/release-inputs.json)。先从维护者取得指定审核材料包，核对SHA-256，再将其`release/python/`子树解到仓库根；材料包不含wheel／模型。目前材料仅附于非公开审核草稿，干净源码克隆本身不能直接构建Windows安装器；该输入约定不代表GPL门槛已解除。

发布预检核对CPython3.11／Windows x64，并以原始哈希锁定wheel为依据验证安装文件，不只信任可改写的已安装RECORD。先执行`python -m pip download --require-hashes --only-binary=:all: --no-deps -r scripts/release-windows-py311.lock -d release/python/wheelhouse`下载完整锁定闭包（不安装）；`release_python.py verify --python <path> --wheelhouse <directory>`及打包`-Wheelhouse <directory>`可指定其他缓存。原始wheel缺失／变更、原有哈希行丢失、安装RECORD或文件内容变更均在替换runtime前拒绝。原始无哈希行及安装器生成附加项不构成来源声明；wheel来源校验不代表GPL对应源码闭环。生成锁仍要求空wheelhouse。

预检还拒绝锁定包目录内未登记的新增文件，避免PyInstaller从RECORD外收集额外代码／数据。两个本机HTTP服务在处理前校验回环Host与已知浏览器Origin，保留无Origin的CLI调用；防护针对外站CSRF／DNS重绑定，不是本机恶意进程的安全沙箱。

## 验证范围

项目包含 Rust/Python 契约及组件检查；路线图记录最近观察到的集成状态。曾在 Windows x64 上使用外部 Python 环境和声库，验证原生 DiffSinger CUDA 会话、CUDA Basic Pitch 转录及 Demucs 分离。这些机器特定结果仅说明该配置下的路径，不保证干净克隆或任意模型/声库可用。

## 已知限制

当前优先级与解除条件见[阻塞清单](docs/dev_roadmap.md#剩余阻塞清单2026-10-21)B01—B10。B01按验收误报关闭；Windows共享／独占、同采样率热切换与Tauri实时VST内嵌已实现并有范围限定的验证。ASIO未实现，物理拔插、真实同名硬件及任意插件兼容仍未验收。模型／公网服务须先具备资产、授权与账号；本地安装不能代替发布许可审核。

B02—B10完整解除条件仍保持开放，不将本轮局部实现记为全项关闭。B01保留追溯，历史退出报告以[主窗口定向验收](docs/dev_roadmap.md#2026-10-20b01退出误报定位与定向验收)更正为准。本轮因新增功能重打包，不是为误报加入产品退出补丁。

- VITS 适配器尚未实现；多说话人和情绪控制未针对兼容模型完成通用验证。
- 同模型DiffSinger Blend将复制的阶段预测器绑定到混合speaker reader，原缓存不变。已用合成向量验证真实声学输入准备；这不等于真实多说话人音频验收，也不代表取得商用声库许可。
- 工具→声库可在接受条款后下载官方绮萱2.7.0原包。中断保留断点，仅匹配Range时追加，忽略Range则安全重下；完整长度／SHA-256匹配后安全解包登记，不覆盖已有目录，原包保留在`.downloads`。不自动安装独立许可声码器或推理环境，安装不代表可推理或商用授权。可用`SVS_VOICE_INSTALL_DIR`覆盖默认目录。
- 自建分发服务入口`apps/voice-distribution/server.py`，提供本地管理员建号、原包及许可材料提交／审批、账号鉴权与签名Range下载。设置`SVS_DISTRIBUTION_HOME`后依次运行`init`、`create-admin <user>`、`create-user <user>`、`serve`；密码交互输入，非回环必须TLS。客户端工具→声库连接指定服务、阅读校验后的许可并显式确认，Windows以DPAPI保存token。已用自有元数据包完成真实认证回环安装，不含第三方模型，不等于公网部署或分发授权。
- 分发确认绑定所选原包与许可SHA-256；刷新、重新连接或修改端点会撤销本机旧确认，服务端哈希变化须重新阅读，不会安装另一审核版本。
- 显式要求 CUDA 时，设备/运行时不可用会报错；音频解码、部分后处理与混音仍可能运行于 CPU。
- VST3支持轨道／总线／Master隔离离线处理及持续有缓冲试听；参数、旁路和电平可在线调节，顺序／路径／路由变更需重播。可信插件放`plugins/`并保存相对路径，第三方插件不随应用分发。Nova2.2.3和WET1.3.0已实测；WET重复／分块结果不逐样本相等，不保证任意插件兼容。Windows Tauri可内嵌当前实时实例GUI。
- 停止时选择输出及共享／独占模式，Windows按系统端点GUID标识设备，缺失目标明确失败、不回退默认。播放中可切换同采样率设备；不同采样率须停止后重建AudioContext。选择只属于本次会话，不写工程；浏览器该路静音，设备队列、消费时钟及欠载独立显示。停止释放、失联约5秒过期。仍有WebAudio／VST／HTTP／设备缓冲，不支持ASIO，不代表DAW级端到端低延迟，不用于远程server。
- 原生路线以WebAudio无声sink驱动源捕获，避免独占默认声卡后浏览器时钟冻结。独占模式要求WebView2支持`AudioContext.setSinkId`；旧宿主明确报错，不能显示播放却无消费帧。
- 试听按路由计算尾音：已启用效果的显式 `tail_seconds` 串联相加，并行路径到 Master 取最长；旁路和未连接效果不延长预算。播放中修改尾音在服务端更新成功后重算停止点；不自动检测插件完整衰减，也不是插件延迟补偿。
- 停止时可选512/1024/2048/4096帧分块、2或4块预缓冲，默认仍2048/4。仅本次会话有效，作用于原生VST处理/声卡路线，不影响无VST普通浏览器播放。小分块增加HTTP请求压力，可能欠载或在持续过载时明确停止；不稳时增大分块/预缓冲。显示的分块预算不包含设备驱动、捕获和网络开销，不是端到端延迟实测。
- Windows退出验收须只关闭PID所属的`Tauri Window`主窗口，不能关闭可见的`Tao Thread Event Target`辅助窗口；此前驻留报告由误关该辅助窗口造成。未改安装器在正常关闭主窗口及原生VST播放中关闭均退出成功。可运行`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_desktop_shutdown.ps1 -Executable <安装目录>/svs-desktop.exe`验证空闲退出、自有进程树及后端端口释放；端口已有服务时启动前拒绝。本轮无需产品退出补丁或重打包。
- 离线VST渲染去除报告样本延迟并排空末尾，扣除宿主已省略的起始缓存。持续试听按固定会话预算对齐轨道输出/发送、嵌套总线及Master；旁路保留预算并持续运行插件及干声延迟，改变插件延迟的参数会停止会话并要求重播。走带分别显示插件补偿与网络缓冲，可听时钟扣除起始补偿以保留曲尾。误报或未报告延迟不能自动纠正。两项补偿已进入当前本地验收的Windows安装器。
- 工具→VST3保留独立编辑器；混音器实时实例编辑器在Windows Tauri内嵌自有worker的JUCE窗口，浏览器仍为独立窗口。会话／PID／修订号／精确HWND归属均校验。关闭回读参数和原生预设，显式应用才停止试听并按原工程修订号提交；并发修改拒绝覆盖。预设随工程／携带包保存，先恢复再叠加参数，上限4MiB，不含外部文件和DSP历史。Nova已实测子窗口归属、关闭回读及显式保存；不保证任意插件兼容，安装器未签名且未解除公开分发许可门槛。
- 离开混音器后迟到的打开结果会被关闭；缩放／脱离要求完整会话／worker／修订号身份。Tauri内嵌自有JUCE内容子窗，不重挂Pedalboard顶层宿主；宿主保持可见并移出屏幕，避免编辑器循环退出。原生region按视口及滚动祖先裁剪内容，空region保持编辑器存活；脱离恢复原父窗、region和位置。冻结worker声明PerMonitorV2并初始化进程／线程DPI上下文。Nova重复挂载／裁剪／恢复／关闭及最小化时挂载已在本机验收，其他DPI环境及任意插件未验收。五秒找不到窗口失败，关闭及时停止发现线程。
- 缩放／脱离额外检查内容当前父窗和原宿主归属；编辑器先关闭时释放保存region并清除挂载状态，避免阻塞下一次打开。六款本机编辑器（Nova、WET、Dragonfly四款）各两轮空裁剪／恢复／重开／状态回读及继续处理通过；不代表任意插件或跨DPI认证。
- WET Reverb 1.3.0及Dragonfly 3.2.10早期反射／Hall／Plate／Room已做预设＋参数、24／44.1／48kHz轨道／总线／Master离线与持续处理验收。预设不能单独代替参数：WET预设未恢复所选模式，Dragonfly写参数后未处理时原生状态仍可能是旧值，须共同保存。本次编辑器生命周期覆盖上述本机版本，但不宣称插件专属可视编辑、任意插件或其他机器均通过。
- 原生试听中可实时调节已有轨道/总线发送的电平，混音器保存后保持原生会话；增加、删除发送或更换发送目标仍须停止重播。
- 原生试听也会预取静音及非独奏轨的短句，解除静音或取消其他轨独奏后可恢复当前短句，无须重播；因此起播合成等待可能增加，仍受原缓存上限约束。静音/独奏变化在已缓冲输出播放后生效。
- 原生试听在线声像同时作用于当前音源及后续短句/音频剪辑，不在下一音源起播时回到旧值；仍需等待已缓冲输出。
- 原生PCM编解码与网络请求使用专用Web Worker，通过MessageChannel直连AudioWorklet，不随界面主线程暂停；音频时钟在无动画帧时仍驱动调度。队列有界，欠载后按序补缓冲，持续过载明确停止；不保证系统休眠或无限主线程停顿期间连续播放。
- 捕获使用七块预分配PCM缓冲，在AudioWorklet与流worker间转移，HTTP处理及可选声卡提交完成后归还；复用前清零，缺席轨道不会残留旧采样。worker停滞耗尽固定池时明确停止，不无限分配或静默丢帧。
- 工具→工程同步支持Windows手动WebDAV整包上传与恢复。配置已存在的HTTPS集合URL及应用密码，凭据以Windows当前用户DPAPI加密。ETag条件更新防止盲目覆盖，冲突另存副本；恢复要求新目录、先备份当前工程与媒体，下载期间工程被编辑则拒绝替换。已验证真实认证回环WebDAV；未验收公网Nextcloud部署及跨机器云恢复，不是自动双向合并。AI主题作曲与伴奏仍使用外部聊天服务。
- WebDAV列表按属性状态及XML命名空间解析，失败属性与同名扩展字段不会覆盖成功的DAV元数据；此兼容性修复不代表公网服务或跨机器验收完成。
- 工具页“乐谱导入/导出”可选择“SVS 工程包（含音频）”：将工程和外部音频打成带 SHA-256 清单的 `.svsbundle`，恢复到尚不存在的新目录；相对源音频路径需填写工程原目录。不会携带声库、模型、插件或凭据，也不会覆盖现有输出；这不是云同步。
- 同步相对媒体路径前须填写工程原目录。本机恢复前备份位于`%APPDATA%/SVS Studio/project-sync/backups/`；断开只删除配置与凭据，不删除备份。声库、模型、插件不上传；安全凭据持久化目前仅支持Windows，其他平台拒绝而非明文保存。
- 本机 GPU 验收不代表仓库源码包含 Python 环境、CUDA、声库、模型或用户歌曲。

## 许可与第三方资源

SVS Studio 原创源码和文档采用 [Apache License 2.0](LICENSE)（SPDX：`Apache-2.0`）。Copyright 2026 Brian98Z and SVS Studio contributors。第三方依赖、模型、插件、声库及外部服务仍遵循各自许可与条款；本项目许可不改变这些资源的许可，也不授予其资产使用权。使用或再分发前请核验对应条款。
