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

源码开发需要 Rust stable 工具链（Windows 使用 MSVC 工具链）、Node.js/npm，以及 Python 3.10+ 与 pip。桌面窗口需要 WebView2。GPU 推理还需要与目标硬件、驱动和依赖版本匹配的 CUDA 运行时，以及相应外部模型和授权声库。

Rust 服务和 CLI 可在不安装可选模型栈时构建。推理服务桌面基础依赖见 `packages/inference-service/requirements-desktop.txt`；`requirements-music-ai.txt` 是可选音乐 AI 依赖。PyInstaller、DiffSinger/OpenUtau 相关包、PyTorch、ONNX Runtime GPU 和 Basic Pitch 等按任务另行配置；GPU 还需兼容驱动/运行时及外部 Python 环境。
媒体导入/导出还可能需要系统 FFmpeg；按需安装相应可选依赖（如 `requirements-media.txt`）。

## 从源码构建与运行

在仓库根目录使用 PowerShell 或兼容 shell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r packages/inference-service/requirements-desktop.txt
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

如需独立启动 Python 推理服务，在服务包目录安装其要求文件并运行：

```sh
cd packages/inference-service
python -m pip install -r requirements-desktop.txt
python -m inference_service.service_runner --port 8760
```

### 可选外部 GPU 歌声运行时

GPU 歌声推理不是干净克隆后即可独立使用的功能。需要自行提供兼容的外部 Python 环境、DiffSinger 模型文件、声库，以及所需的 `abc2diffsinger` 集成/插件和 `diffsinger_utau` 运行依赖。通过 `SVS_ABC2VOCALS_DIR` 指定外部插件目录；启动 server 前，将 `SVS_GPU_PYTHON` 设为 Python 可执行文件路径，将 `SVS_VOICEBANK` 设为经授权的声库目录，并设置 `SVS_INFERENCE_DEVICE=cuda`。源码中的插件默认路径属于开发机器，其他机器应明确覆盖。显式 CUDA 初始化错误会报告失败，不会静默回退 CPU；CPU DSP 是另一条独立路径。未经资源权利人许可及适用许可授权，不得分发模型或声库。

## CLI 与 MCP

`svs-cli` 向本机 `127.0.0.1:8761` 的 server 发送请求，不是独立工程编辑器。不带参数运行会打印命令摘要并以错误状态退出；以下示例对应 CLI 命令解析器：

```sh
./target/release/svs-cli new
./target/release/svs-cli track add sing Lead
./target/release/svs-cli note add <track-id> --tick 0 --dur 480 --key 60 --lyric Hello
./target/release/svs-cli render output.wav
```

Windows 使用 `target/release/svs-cli.exe`。MCP server 是面向兼容客户端的独立 stdio 进程；在 `apps/mcp-server` 目录安装其依赖并启动：

```sh
cd apps/mcp-server
python -m pip install -e .
python server.py
```

将 MCP 客户端配置为以上启动命令及对应工作目录。CLI/MCP 仅支持各自接口已实现的操作；当前能力请参考其源码和契约。

## Windows 桌面打包

打包入口为 `scripts/package_desktop.ps1`。构建需上述 Rust/MSVC 与 Node 工具链，以及 Tauri 的 Windows 前置条件。Windows 系统可能需要 WebView2 Runtime。打包不会包含第三方声库、合成模型、CUDA 运行时，也不保证附带可用的 GPU Python 环境；使用原生 GPU 合成须另行配置兼容的外部环境和已授权资源。

打包使用独立环境 `apps/desktop/src-tauri/.venv-build`：通过 `python -m venv apps/desktop/src-tauri/.venv-build` 创建，再使用该环境的 Python 安装 `packages/inference-service/requirements-desktop.txt` 与 `pyinstaller`。运行打包脚本前先构建前端。应用与构建停止后，可删除自动生成的 `src-tauri/runtime/`、`src-tauri/gen/`、Python/pytest 缓存与 PCM 缓存，使用时会重新生成；须保留打包环境、锁文件、最终安装器、用户工程、声库及验收证据。清除 PCM 缓存后首次合成将重新推理。当前脚本实际使用 `.venv-build` 冻结运行时，不使用可选参数 `-PythonExecutable` 指定的环境。

## 验证范围

项目包含 Rust/Python 契约及组件检查；路线图记录最近观察到的集成状态。曾在 Windows x64 上使用外部 Python 环境和声库，验证原生 DiffSinger CUDA 会话、CUDA Basic Pitch 转录及 Demucs 分离。这些机器特定结果仅说明该配置下的路径，不保证干净克隆或任意模型/声库可用。

## 已知限制

- VITS 适配器尚未实现；多说话人和情绪控制未针对兼容模型完成通用验证。
- 显式要求 CUDA 时，设备/运行时不可用会报错；音频解码、部分后处理与混音仍可能运行于 CPU。
- VST3 属于离线处理器路径；尚未验证 GUI 插件嵌入或完整效果链。
- 云同步尚未实现。AI 主题作曲和伴奏调用已配置的外部聊天服务，不是本地 GPU LLM。
- 本机 GPU 验收不代表仓库源码包含 Python 环境、CUDA、声库、模型或用户歌曲。

## 许可与第三方资源

SVS Studio 原创源码和文档采用 [Apache License 2.0](LICENSE)（SPDX：`Apache-2.0`）。Copyright 2026 Brian98Z and SVS Studio contributors。第三方依赖、模型、插件、声库及外部服务仍遵循各自许可与条款；本项目许可不改变这些资源的许可，也不授予其资产使用权。使用或再分发前请核验对应条款。
