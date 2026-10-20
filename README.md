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

Install canonical Python profiles from the repository root: `requirements.txt` is the CPU desktop runtime aggregate; `requirements-core.txt` is shared runtime; `requirements-build.txt` adds PyInstaller; and `requirements-mcp.txt` is for the stdio MCP server. Optional profiles are `requirements-g2p.txt` (multilingual G2P), `requirements-media.txt` (AAF interchange), `requirements-vst.txt` (offline VST processing), `requirements-music-ai.txt` (Basic Pitch/Demucs/PyTorch), `requirements-gpu.txt` (GPU inference), `requirements-native.txt` (legacy external DiffSinger integration requirements), and `requirements-dev.txt` (development/test dependencies). Install only the profiles needed for your workflow; VST remains optional and is not included in the default runtime. These files express version constraints, not a complete all-platform hash lock. Cargo/npm manifests and lockfiles remain authoritative for Rust and JavaScript dependencies. `requirements-system.txt` inventories external system tools; pip cannot install Rust, npm, FFmpeg/ffprobe, models, or voicebanks.
CPU and GPU ONNX Runtime distributions must not coexist in one environment. For music-AI GPU use, create a clean separate venv and install `requirements-gpu.txt`; do not first install `requirements.txt`, which includes CPU `onnxruntime`. For native DiffSinger CUDA synthesis use the separate `requirements-native-gpu.txt` setup below. The legacy `requirements-native.txt` remains CPU-only: upstream 0.3.8 requires old CPU ORT/Torch on Python 3.11. Do not combine these profiles or mutate installed package metadata to force compatibility.
`requirements-music-ai.txt` includes Basic Pitch and Demucs/PyTorch. On Python 3.11, Basic Pitch 0.4 metadata requires TensorFlow >=2.4.1,<2.15.1 even though the project uses its ONNX path; a normal resolver therefore installs TensorFlow as well.
Media import/export requires FFmpeg/ffprobe on PATH for relevant operations.

## Build and run from source

From the repository root, in PowerShell or a compatible shell:

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

Start the server from the repository root; it serves the built desktop frontend at `http://127.0.0.1:8761` and normally starts the inference worker as a child process:

```sh
./target/release/svs-server
```

On Windows, use `target/release/svs-server.exe`. `--no-inference` only skips starting a worker child process; synthesis requests can still use a worker already available at the configured inference endpoint.

```powershell
.\target\release\svs-server.exe --no-inference
```

The server's worker startup uses the CPU desktop requirements above. To run a worker separately, activate that environment and launch it from the inference-service package directory:

```powershell
cd packages/inference-service
python -m inference_service.service_runner --port 8760
```

The optional feature profiles listed above are installed from the repository root as needed.

The Windows/Python 3.11 `requirements-gpu.txt` profile has been installed in a new environment and exercised on an RTX 5070 Ti for CUDA Basic Pitch and Demucs HTTP requests, with `pip check` passing. It installs Basic Pitch's declared TensorFlow dependency as well. This modern music-AI profile does not include all legacy native DiffSinger integration dependencies; configure the native voice environment separately below. Cached official model weights were reused for that verification, not bundled in this repository.


When needed for ABC import or native voice synthesis, set `SVS_ABC2VOCALS_DIR` to an external compatible `abc2diffsinger` checkout; this dependency is not included here. Its default path in the current source is machine-specific, so configure the environment variable on a fresh installation.

### Optional external GPU voice runtime

GPU voice support is not a fresh-clone, self-contained setup. Provide a compatible external Python environment, DiffSinger model files and voicebank, and the required `abc2diffsinger` integration/plugin (`diffsinger_utau` for the verified native runtime). Configure `SVS_ABC2VOCALS_DIR` to the external plugin's location and set `SVS_GPU_PYTHON` to the compatible Python executable and `SVS_VOICEBANK` to the licensed voicebank directory before launching the server. GPU configuration uses `SVS_INFERENCE_DEVICE=cuda`; explicit CUDA setup errors are reported rather than silently falling back to CPU. CPU DSP remains available as a separate route. Do not distribute models or voicebanks without their owners' permission and applicable licenses.

On Windows x64, install Python 3.11 and `uv`, then run from the repository root (the destination must not exist):

```powershell
.\scripts\setup_native_cuda.ps1 -EnvironmentDirectory C:\SVS\native-cuda -PythonExecutable C:\Python311\python.exe
$env:SVS_GPU_PYTHON = 'C:\SVS\native-cuda\Scripts\python.exe'
```

The script builds `diffsinger-utau==0.3.8+svscuda1` from a SHA-256-pinned MIT upstream source archive, preserving runtime Python bytes and license while explicitly changing build-time dependency metadata for CUDA. It normally resolves all dependencies through pip and runs `pip check`; it does not use `--no-deps` or rewrite installed metadata. Generated wheels and a source manifest live under `build/native-cuda/wheels`; rebuild after removing that generated output if needed. This profile selects Torch 2.11.0+cu130, ORT GPU 1.31.0, NumPy 1.26.4 and SciPy 1.13.1. It excludes Basic Pitch/Demucs and does not provision the external ABC integration, voicebank, vocoder, driver or models. Downloads can be several GB. Dependency constraints are not a complete transitive hash lock or a guarantee for other machines.

Registered DiffSinger voices can reference an existing external vocoder without changing the voicebank archive: Tools → Voices → enter the directory containing `vocoder.yaml`, read its independent license, acknowledge it, and bind. The model name and acoustic/mel parameters must match, and the referenced ONNX must exist within that directory. Clearing the binding removes only the reference. Configure the external runtime before launching; after binding, re-synthesize and replay. Bindings are local registry settings, not portable project assets. Prerequisite status is not proof of synthesis or commercial permission; no vocoder weights or runtime are automatically downloaded.
The current Windows installer was verified after isolated installation: binding/clearing works in the bundled frozen runtime; real Qixuan synthesis requires a separately configured external GPU Python and compatible vocoder. Independently, the native CUDA setup above was installed into two new Windows environments and exercised for real 24 kHz synthesis with eight CUDA sessions. These are separate installer and source-service observations, not a bundled GPU environment, other-machine guarantee, commercial permission, or arbitrary voice compatibility.

## CLI and MCP

`svs-cli` sends requests to the local server at `127.0.0.1:8761`; it is not a standalone project editor. It prints a command summary when invoked without arguments. Examples matching the CLI command parser:

```sh
./target/release/svs-cli new
./target/release/svs-cli track add sing Lead
./target/release/svs-cli note add <track-id> --tick 0 --dur 480 --key 60 --lyric Hello
./target/release/svs-cli render output.wav
```

The MCP server requires `requirements-mcp.txt` and runs as a separate stdio process for MCP-compatible clients. From the repository root:

```sh
python -m pip install -r requirements-mcp.txt
python apps/mcp-server/server.py
```

Only operations implemented by the respective CLI/MCP interfaces are available; consult their source and the contract for current details.

## Windows desktop packaging

The packaging entry point is `scripts/package_desktop.ps1`. Building the desktop application requires the Rust/MSVC and Node toolchains above, plus Tauri's Windows prerequisites. A Windows installation may require WebView2 Runtime. Packaging does not bundle third-party voicebanks, synthesis models, a CUDA runtime, or a guaranteed working GPU Python environment. A compatible external environment and licensed assets must be configured separately to use native GPU synthesis.

Packaging uses a dedicated build environment at `apps/desktop/src-tauri/.venv-build`. Create it with `python -m venv apps/desktop/src-tauri/.venv-build`, then activate it and install `requirements-build.txt` from the repository root. Build the frontend before running the packaging script. Generated `src-tauri/runtime/`, `src-tauri/gen/`, Python/pytest caches, and PCM caches may be removed while the application and builds are stopped; they are recreated when needed. Preserve the build environment, lockfiles, final installers, user projects, voicebanks, and acceptance evidence. Removing PCM caches makes the next synthesis a cache miss. The script currently uses `.venv-build` for freezing, not its optional `-PythonExecutable` parameter.

The frozen build profile includes the Pedalboard VST3 host and its native runtime. Plugin binaries remain external and are not included. Installed applications may use `SVS_PLUGIN_DIR` to select a trusted plugin folder; the portable project bundle preserves plugin references but does not carry plugins.

Pedalboard 0.9.26 is GPLv3-licensed. The application's Apache-2.0 source license does not remove the obligations of a binary distribution containing it. Public installer redistribution requires a separate GPL compliance review, license notices and applicable corresponding source; a locally verified installer is not evidence that this release gate has been cleared.

## Verification

The project has Rust and Python conformance and component checks; the roadmap records the latest observed integration status. A recorded Windows x64 GPU setup used an external Python environment and voicebank to exercise native DiffSinger CUDA sessions, CUDA Basic Pitch transcription, and Demucs separation. Those machine-specific results are evidence for that configured setup, not a guarantee that a clean checkout or arbitrary model/voicebank will work.

## Known limitations

Current blocker priorities and closure criteria are tracked in the [remaining blocker ledger](docs/dev_roadmap.md#剩余阻塞清单2026-10-20), B01–B10. Fix the reproduced desktop-process shutdown defect first, then audio stability/latency; model and public-service work requires the stated assets, rights or accounts. ASIO/exclusive mode, live device switching and embedded VST editors remain unimplemented; physical unplugging and real duplicate-name devices remain unverified. Public distribution is gated by licensing and clean-machine acceptance, not local installation alone. This documentation update does not clear any gate or rebuild the installer.

- VITS adapter support is not implemented. Multispeaker and emotion controls are not verified across compatible models.
- Same-model DiffSinger blends bind copied stage predictors to the blended speaker readers, keeping cached originals unchanged. Real acoustic input preparation was exercised with synthetic vectors; this is not trained multispeaker audio validation or a commercial voicebank license.
- Tools → Voices offers the pinned official Qixuan 2.7.0 archive after explicit acceptance of its terms. Interrupted downloads survive application restarts: accept the terms again and choose Continue download to resume the saved bytes. A matching HTTP range is required for appending; servers that ignore Range trigger a safe restart. Length and full SHA-256 must match before full safe extraction and registration; existing installations are not overwritten. The `.downloads` cache under the voice install directory retains original archives for reuse. No rehosting, account service, independently licensed vocoder or inference environment is bundled or auto-installed. Installing a voicebank does not establish inference readiness or grant every commercial use. `SVS_VOICE_INSTALL_DIR` overrides the default local data directory.
- VST3 supports isolated offline processing and persistent, buffered audition for tracks, buses and the master. Audition processes blocks continuously, not a pre-rendered song; parameters, bypass and mixer levels can change during playback. Changing plugin order/path or routing requires restarting playback. The transport reports buffering and underruns; this is not a low-latency sound-card callback host. Place trusted plugins in `plugins/`; saved paths are relative. Chinese labels in `apps/desktop/src/audio/effect-labels.zh-CN.json` preserve raw parameter keys and values. TDR Nova 2.2.3 and WET Reverb 1.3.0 were exercised for offline processing, with WET tails at 24/44.1/48 kHz. WET repeated/block-size runs are not bit-identical; arbitrary-plugin compatibility is not guaranteed. Native GUI embedding is unsupported; third-party plugins are not redistributed.
- While stopped, select Browser output or Native soundcard output, then refresh and choose a native output device (or System default). Native output uses that device's default sample rate through a bounded CPAL callback queue; switching rates rebuilds the source audio context. Explicit missing or ambiguous devices fail rather than falling back; device-name changes require refreshing/reselecting. Selection is session-local, not saved in projects. The browser route stays silent; device queue/underruns and consumed-frame clock are shown. Stop releases the device; lost clients expire after about five seconds. This remains a buffered WebAudio → isolated VST worker → HTTP → device path, not ASIO/exclusive mode or measured low-latency DAW playback. Do not use it with a remote server.
- Audition tail budgets add the enabled effects' explicit `tail_seconds` along each routed path and take the longest parallel path to the master. Bypassed/disconnected effects do not extend the budget; successful live tail edits recalculate the stopping point. This does not detect a plugin's full decay or compensate plugin latency.
- While stopped, audition blocks can be 512/1024/2048/4096 frames with 2 or 4 prebuffer blocks; the default remains 2048/4. These session-only settings apply to native VST processing and soundcard output, not ordinary browser playback without VST. Smaller blocks increase HTTP/request pressure and may underrun or stop explicitly on sustained overload; increase the block size/prebuffer if unstable. Displayed block budgets exclude device-driver, capture and network overhead, and are not end-to-end latency measurements.
- Current Windows acceptance observed a shutdown limitation: closing the window stopped the backend services, but the desktop process remained after 15 seconds. Normal process exit is not verified for this installer.
- Offline VST rendering removes reported sample delay and drains the signal end, accounting for startup samples already withheld by the host. Continuous audition aligns track outputs/sends, nested buses and the master using a fixed session delay budget. Bypass preserves that budget and keeps the plugin/dry delay running; latency-changing parameters stop the session and require restarting playback. The transport displays plugin compensation separately from network buffering and delays its audible clock until startup has passed. Unreported or inaccurate plugin latency cannot be corrected. These compensation changes are included in the current locally verified Windows installer.
- Tools → VST3 retains its isolated standalone native editor. The mixer also opens a native editor on the actual running audition instance for a track, bus or master effect; edits affect subsequent buffered audio without automatically modifying the project. After closing, explicit confirmation stops playback and commits captured parameters plus the native preset against the original project revision; intervening project edits reject the commit. Presets persist in projects/bundles and restore before parameter overrides. Presets are bounded to 4 MiB and checked for format and plugin class; external samples/files, unexported data and running DSP history are excluded. Windows Nova native mouse editing from −12 to −6 dB, same-session PCM, UI confirmation and save/reopen were exercised in the current Windows installer after isolated installation, using its bundled frozen runtime without external Python. GUI embedding, arbitrary-plugin/version compatibility and low-latency sound-card hosting are not claimed. The local installer is unsigned; no public release or third-party redistribution clearance is claimed.
- WET Reverb 1.3.0 and Dragonfly 3.2.10 Early Reflections/Hall/Plate/Room were additionally exercised with saved presets plus parameter overrides, offline track/bus/master processing at 24/44.1/48 kHz, and continuous sessions. Presets alone are not sufficient: WET did not restore its selected mode, and Dragonfly parameter writes were not reflected in captured state until processing. Keep the preset and parameters together. The current installer fixes mixer normalization dropping those overrides; earlier native-state installer evidence did not cover this case. Dragonfly Early Reflections editor/project readback and the installed frozen-runtime fix were exercised; other native editors remain unverified.
- Track and bus send levels can change during native audition without restarting the session, including changes saved through the mixer UI. Adding/removing sends or changing destinations still requires restarting playback.
- Native audition prepares phrases for muted and non-solo tracks too, so unmuting or releasing solo can restore the current phrase without restarting playback. These phrases can increase initial synthesis wait and consume the existing bounded cache; mute/solo changes are heard after already-buffered output.
- Live native track pan applies to both active sources and later phrases/audio clips in the same session; it does not reset when the next source starts. Changes are heard after already-buffered output.
- Native PCM networking runs in a dedicated Web Worker connected directly to the AudioWorklet, rather than the renderer thread. Audio-clock messages keep native scheduling active without animation frames. Queues remain bounded; underruns refill in order, while sustained overload stops explicitly. This does not guarantee uninterrupted playback during system sleep or indefinite renderer stalls.
- Tools → Project sync supports manual WebDAV bundle upload and restore on Windows. Supply an existing HTTPS collection URL and an application password; credentials are protected by current-user Windows DPAPI. Conditional ETags prevent blind overwrites and preserve conflicting uploads under separate names. Restore requires a new directory, backs up the current project and media, and refuses to replace a project edited during download. Real authenticated loopback WebDAV was exercised; no hosted Nextcloud deployment or cross-machine cloud recovery is claimed. This is not automatic bidirectional merging.
- WebDAV listings honor each property's status and XML namespace; unavailable properties and same-name extension fields do not overwrite successful DAV metadata. This compatibility fix does not establish public-server or cross-machine acceptance.
- Tools → score import/export supports SVS project bundles (`.svsbundle`): a project snapshot plus referenced audio, deduplicated and checked against a SHA-256 manifest. Relative source audio requires an explicit project base directory; restoration requires a new directory and refuses overwrites. Voicebanks, models, plugins and credentials are not included. This is portable backup/restore, not cloud synchronization.
- For relative media paths, provide the project base directory before sync. Local pre-restore bundles remain in `%APPDATA%/SVS Studio/project-sync/backups/`; disconnect removes the saved profile/credential, not these backups. Voicebanks, models and plugins are not uploaded. Secure credential persistence currently requires Windows; other platforms refuse rather than store plaintext.
- AI composition/accompaniment depends on an externally configured chat/model service, not a bundled local LLM.
- GPU model inference requires external compatible dependencies and assets; not every audio operation runs on GPU.

## Licensing and third-party assets

SVS Studio's original source code and documentation are licensed under the [Apache License, Version 2.0](LICENSE) (SPDX: `Apache-2.0`). Copyright 2026 Brian98Z and SVS Studio contributors. Third-party dependencies, models, plugins, voicebanks, and external services retain their own licenses and terms; this project's license does not relicense them or grant rights to their assets. Verify their terms before use or redistribution.
