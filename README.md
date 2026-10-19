# SVS Studio

**Version 0.1.0** · An independent, local-first singing-voice synthesis and music-editing workstation.

[简体中文 / Chinese](README.zh-CN.md)

## What works

- Multi-track projects with a piano-roll editor, lyrics, tempo maps, expression curves, and undo/redo.
- Local singing synthesis through a generic DSP voice and an optional external native DiffSinger/ONNX runtime. Synthesis uses phrase requests and caching, not sample-by-sample neural streaming.
- Phrase playback and mixing through Web Audio, with multiple tracks, buses/sends, stereo processing, and 24 kHz stereo float WAV export.
- MIDI, MusicXML, LRC, UfData and project/media interchange paths, plus CLI and MCP access to supported server operations.
- Optional local GPU workflows for native DiffSinger voice synthesis, Basic Pitch vocal-to-MIDI, and Demucs source separation when their compatible external runtime, models, and assets are supplied.
- Theme-based lyrics/song generation and accompaniment through a configured external chat/model service. These features are not a locally hosted GPU language model.

## Architecture

```text
Vue desktop (Tauri) ── REST/WebSocket :8761 ── Rust server ── HTTP :8760 ── Python inference service
        │                    ▲                      │                       └─ DSP / optional ONNX and native voice adapters
        ├── svs-cli ─────────┘                      └─ project, render and interchange coordination
        └── MCP server (stdio) ─────────────────────── supported server operations
```

Rust `svs-core` owns shared project/time/curve logic; `apps/server` hosts the API and serves the desktop frontend; `apps/cli` is an HTTP client; `apps/mcp-server` exposes supported operations to MCP clients. `packages/inference-service` provides audio processing and optional model adapters. The shared behavioral contract is documented in [docs/CONTRACT.md](docs/CONTRACT.md); development status and roadmap are in [docs/dev_roadmap.md](docs/dev_roadmap.md) (these project documents are not necessarily translated).

## Prerequisites

For a source build, install:

- Rust stable toolchain and Cargo; on Windows use the MSVC toolchain and its required C++ build tools.
- Node.js/npm for the desktop frontend.
- Python 3.10+ and pip for the inference service (the exact supported dependency versions are defined by its requirements files).
- WebView2 Runtime to run the Tauri desktop app on Windows; it may already be installed by the operating system.

The Rust server and CLI can be built without the optional model stacks. The base inference-service requirements are `packages/inference-service/requirements-desktop.txt`; music-AI features have additional requirements in `requirements-music-ai.txt`, and optional G2P/interchange dependencies are listed in `packages/g2p-engine/requirements.txt` and `packages/interchange/requirements.txt`. PyInstaller, DiffSinger/OpenUtau-related packages, PyTorch, ONNX Runtime and Basic Pitch are optional, task-specific dependencies and are not all installed by the base requirements. GPU use requires a compatible GPU driver/runtime and compatible external Python environment; having a CUDA-capable GPU alone is insufficient.
Media import/export additionally requires FFmpeg/ffprobe on PATH for the relevant operations. Optional Python media dependencies are listed in `packages/inference-service/requirements-media.txt`.

## Build and run from source

From the repository root, in PowerShell or a compatible shell:

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

Start the server from the repository root; it serves the built desktop frontend at `http://127.0.0.1:8761` and normally starts the inference worker as a child process:

```sh
./target/release/svs-server
```

On Windows, use `target/release/svs-server.exe`. `--no-inference` only skips starting a worker child process; synthesis requests can still use a worker already available at the configured inference endpoint.

```powershell
.\target\release\svs-server.exe --no-inference
```

The server's worker startup uses the desktop requirements above. To run a worker separately, install those requirements in its Python environment and launch it from the package directory:

```powershell
cd packages/inference-service
python -m inference_service.service_runner --port 8760
```


When needed for ABC import or native voice synthesis, set `SVS_ABC2VOCALS_DIR` to an external compatible `abc2diffsinger` checkout; this dependency is not included here. Its default path in the current source is machine-specific, so configure the environment variable on a fresh installation.

### Optional external GPU voice runtime

GPU voice support is not a fresh-clone, self-contained setup. Provide a compatible external Python environment, DiffSinger model files and voicebank, and the required `abc2diffsinger` integration/plugin (`diffsinger_utau` for the verified native runtime). Configure `SVS_ABC2VOCALS_DIR` to the external plugin's location and set `SVS_GPU_PYTHON` to the compatible Python executable and `SVS_VOICEBANK` to the licensed voicebank directory before launching the server. GPU configuration uses `SVS_INFERENCE_DEVICE=cuda`; explicit CUDA setup errors are reported rather than silently falling back to CPU. CPU DSP remains available as a separate route. Do not distribute models or voicebanks without their owners' permission and applicable licenses.

## CLI and MCP

`svs-cli` sends requests to the local server at `127.0.0.1:8761`; it is not a standalone project editor. It prints a command summary when invoked without arguments. Examples matching the CLI command parser:

```sh
./target/release/svs-cli new
./target/release/svs-cli track add sing Lead
./target/release/svs-cli note add <track-id> --tick 0 --dur 480 --key 60 --lyric Hello
./target/release/svs-cli render output.wav
```

On Windows, use `target/release/svs-cli.exe`. The MCP server requires the dependencies declared in `apps/mcp-server/pyproject.toml` and runs as a separate stdio process for MCP-compatible clients:

```sh
cd apps/mcp-server
python -m pip install -e .
python server.py
```

Only operations implemented by the respective CLI/MCP interfaces are available; consult their source and the contract for current details.

## Windows desktop packaging

The packaging entry point is `scripts/package_desktop.ps1`. Building the desktop application requires the Rust/MSVC and Node toolchains above, plus Tauri's Windows prerequisites. A Windows installation may require WebView2 Runtime. Packaging does not bundle third-party voicebanks, synthesis models, a CUDA runtime, or a guaranteed working GPU Python environment. A compatible external environment and licensed assets must be configured separately to use native GPU synthesis.

Packaging uses a dedicated build environment at `apps/desktop/src-tauri/.venv-build`. Create it with `python -m venv apps/desktop/src-tauri/.venv-build`, then use that environment's Python to install `packages/inference-service/requirements-desktop.txt` and `pyinstaller`. Build the frontend before running the packaging script. Generated `src-tauri/runtime/`, `src-tauri/gen/`, Python/pytest caches, and PCM caches may be removed while the application and builds are stopped; they are recreated when needed. Preserve the build environment, lockfiles, final installers, user projects, voicebanks, and acceptance evidence. Removing PCM caches makes the next synthesis a cache miss. The script currently uses `.venv-build` for freezing, not its optional `-PythonExecutable` parameter.

## Verification

The project has Rust and Python conformance and component checks; the roadmap records the latest observed integration status. A recorded Windows x64 GPU setup used an external Python environment and voicebank to exercise native DiffSinger CUDA sessions, CUDA Basic Pitch transcription, and Demucs separation. Those machine-specific results are evidence for that configured setup, not a guarantee that a clean checkout or arbitrary model/voicebank will work.

## Known limitations

- VITS adapter support is not implemented. Multispeaker and emotion controls are not verified across compatible models.
- VST3 support is limited to an offline processor path; embedded plugin UI and a fully verified effect chain are not claimed.
- Cloud project synchronization is not implemented.
- AI composition/accompaniment depends on an externally configured chat/model service, not a bundled local LLM.
- GPU model inference requires external compatible dependencies and assets; not every audio operation runs on GPU.

## Licensing and third-party assets

SVS Studio's original source code and documentation are licensed under the [Apache License, Version 2.0](LICENSE) (SPDX: `Apache-2.0`). Copyright 2026 Brian98Z and SVS Studio contributors. Third-party dependencies, models, plugins, voicebanks, and external services retain their own licenses and terms; this project's license does not relicense them or grant rights to their assets. Verify their terms before use or redistribution.
