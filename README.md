# SVS Studio

**Version 0.1.0** · An independent, local-first singing-voice synthesis and music-editing workstation.

[简体中文 / Chinese](README.zh-CN.md)

**Release status:** no public installer release is cleared yet. The maintainer review draft is not a public download; see [audit and release gate](docs/release-audit.md). GitHub Source code archives contain source, not an installed application.

## What works

- Multi-track projects with a piano-roll editor, lyrics, tempo maps, expression curves, and undo/redo.
- Local singing synthesis through a generic DSP voice and an optional external native DiffSinger/ONNX runtime. Synthesis uses phrase requests and caching, not sample-by-sample neural streaming.
- Phrase playback and mixing through Web Audio, with multiple tracks, buses/sends, stereo processing, and 24 kHz stereo float WAV export.
- Continuous VST audition uses binary PCM, isolated plugin workers and optional native output. Windows supports WASAPI shared/exclusive modes and same-rate live device switching; other platforms retain CPAL shared output. Built-in EQ/reverb preserve block state. Default buffering remains 2048 frames / 4 blocks; this is not measured end-to-end latency. ASIO is not implemented.
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

Packaging and release preparation default to `apps/desktop/src-tauri/.venv-release-lock/Scripts/python.exe`; `-BuildPython <path>` selects another packaging environment, which must pass the same hash-locked wheel verification. Create the default environment with CPython 3.11 (`py -3.11 -m venv apps/desktop/src-tauri/.venv-release-lock`) and install `scripts/release-windows-py311.lock` with `--require-hashes`. The packaging script builds the frontend itself. Generated `src-tauri/runtime/`, `src-tauri/gen/`, Cargo targets, PyInstaller work directories and Python/pytest caches can be removed while applications/builds are stopped. Run the full packaging entry point to regenerate runtime before invoking Tauri directly. Preserve the locked build environment, original wheels, license/source inputs, lockfiles, current installer, immediate rollback, user projects, voicebanks and acceptance evidence. Clearing PCM caches causes synthesis to run again.

Run prerequisite inspection with `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package_desktop.ps1 -PrepareOnly`; omit `-PrepareOnly` for packaging. `-Wheelhouse <directory>` is optional and overrides the repository-local cache. Defaults work when invoked from another directory. Text license discovery preserves vendor notices and extension-agnostic license files while excluding Python helpers and binary files. In-place installation removes six obsolete application-owned license helper paths from older packages; it does not clear the runtime or user directory wholesale.

The frozen build profile includes the Pedalboard VST3 host and its native runtime. Plugin binaries remain external and are not included. Installed applications may use `SVS_PLUGIN_DIR` to select a trusted plugin folder; the portable project bundle preserves plugin references but does not carry plugins.

All VST references, including absolute paths restored from projects, must resolve inside the locally configured trusted plugin directory. UNC/device paths and resolved escapes are rejected before loading. Importing a project does not authorize native code outside that root. DiffSinger model references must likewise stay local and within the voicebank before inspection or predictor creation; selecting an external vocoder remains an explicit separate action.

Pedalboard 0.9.26 is GPLv3-licensed. The application's Apache-2.0 source license does not remove the obligations of a binary distribution containing it. Public installer redistribution requires a separate GPL compliance review, license notices and applicable corresponding source; a locally verified installer is not evidence that this release gate has been cleared.

Windows CPython 3.11 desktop builds use `scripts/release-windows-py311.lock` (47 version/hash-pinned wheels) in a clean `.venv-release-lock`, installed with `pip install --require-hashes -r scripts/release-windows-py311.lock`. Packaging checks installed versions and copies third-party license inventories. `release/python` preserves Pedalboard tag source and five exact gitlink archives with hashes; these are source candidates, not proof of the PyPI wheel's exact corresponding source or completed GPL review. Other machines/platforms remain unverified.

A source checkout deliberately excludes `release/`. Required packaging metadata and every companion-file hash are listed in [docs/release-inputs.json](docs/release-inputs.json). Obtain the named review companion from the maintainer, verify its SHA-256 and extract its `release/python/` subtree at repository root before packaging; it excludes wheels/models. The companion is currently attached only to a non-public review draft, so a fresh checkout alone cannot build the Windows installer. This input contract is separate from GPL clearance.

Release preflight verifies CPython 3.11/Windows x64 and installed files against the original hash-locked wheels, not mutable installed RECORD alone. Prepare the local cache with `python -m pip download --require-hashes --only-binary=:all: --no-deps -r scripts/release-windows-py311.lock -d release/python/wheelhouse`; this downloads the complete locked closure without installing it. `release_python.py verify --python <path> --wheelhouse <directory>` and packaging `-Wheelhouse <directory>` accept an alternate cache. Missing/changed wheels, missing original hashed rows, changed installed RECORD or file bytes fail before runtime replacement. Unhashed original rows and installer-generated additions are not provenance claims; wheel provenance does not establish GPL corresponding-source compliance. Lock generation requires an empty wheelhouse.

Release preflight also rejects untracked additions inside locked package trees, because PyInstaller collects filesystem contents beyond installed RECORD. Local HTTP services validate loopback Host and known browser Origin before handlers; CLI calls without Origin remain supported. This blocks website CSRF/rebinding, not a malicious local process or an untrusted application already running as the user.

## Verification

The project has Rust and Python conformance and component checks; the roadmap records the latest observed integration status. A recorded Windows x64 GPU setup used an external Python environment and voicebank to exercise native DiffSinger CUDA sessions, CUDA Basic Pitch transcription, and Demucs separation. Those machine-specific results are evidence for that configured setup, not a guarantee that a clean checkout or arbitrary model/voicebank will work.

## Known limitations

Current priorities and closure criteria are tracked in the [blocker ledger](docs/dev_roadmap.md#剩余阻塞清单2026-10-21), B01–B10. B01 is closed as an acceptance-harness false positive. Windows WASAPI shared/exclusive output, same-rate switching and Tauri live VST editor embedding are implemented with bounded verification; physical unplugging, real duplicate-name hardware and arbitrary-plugin compatibility remain unverified. Models/public services still require the stated assets, rights and accounts; local installation does not clear redistribution gates.

The broad B02–B10 closure criteria remain open despite the implemented subsets. B01 keeps its identifier for traceability; its earlier shutdown report is superseded by the [targeted main-window acceptance](docs/dev_roadmap.md#2026-10-20b01退出误报定位与定向验收). This round rebuilds the installer for new functionality, not a product shutdown patch.

- VITS adapter support is not implemented. Multispeaker and emotion controls are not verified across compatible models.
- Same-model DiffSinger blends bind copied stage predictors to the blended speaker readers, keeping cached originals unchanged. Real acoustic input preparation was exercised with synthetic vectors; this is not trained multispeaker audio validation or a commercial voicebank license.
- Tools → Voices offers the pinned official Qixuan 2.7.0 archive after explicit acceptance of its terms. Interrupted downloads retain bytes across restarts; matching HTTP ranges append, ignored ranges restart safely. Full length/SHA-256 must match before safe extraction and registration; existing installations are never overwritten. The original archive remains in `.downloads`. No independently licensed vocoder or inference environment is auto-installed. Installation does not grant commercial rights or establish inference readiness.
- VST3 supports isolated offline processing and persistent buffered audition for tracks, buses and master. Parameters, bypass and levels may change live; plugin order/path or routing changes require restart. Trusted plugins belong in `plugins/`, saved relatively; third-party plugins are not redistributed. Nova 2.2.3 and WET Reverb 1.3.0 were exercised; WET repeated/block-size results are not bit-identical. Windows Tauri can embed the current live editor; arbitrary-plugin compatibility is not guaranteed.
- Select native output and shared/exclusive mode while stopped. Windows uses system endpoint GUIDs; missing targets fail without falling back. During playback, same-rate device switching preserves the stream; rate changes require stopping and rebuilding the AudioContext. Mode/device selections are session-local. Device queues, consumed-frame clock and underruns are shown; the browser route stays silent. Stop releases the endpoint and lost clients expire after about five seconds. This remains buffered WebAudio → isolated VST → HTTP → device playback, not ASIO or proven low-latency DAW output. Do not use with a remote server.
- Native output uses a silent WebAudio sink for source capture so exclusive output does not stall the browser clock. Exclusive mode requires `AudioContext.setSinkId` support in WebView2; unsupported hosts fail explicitly instead of displaying playback with zero consumed frames.
- Self-hosted voice distribution is available through `apps/voice-distribution/server.py`: local admin account creation, package submission/license evidence, approval and authenticated signed/Range downloads. Set `SVS_DISTRIBUTION_HOME`, run `init`, `create-admin <user>`, `create-user <user>`, and `serve`; passwords are prompted. Non-loopback binding requires TLS. Tools → Voices connects to the chosen server, displays verified terms, requires explicit acceptance and stores Windows tokens with DPAPI. Real authenticated loopback installation used self-authored metadata only; no public deployment or third-party model redistribution is claimed.
- Distribution acceptance is bound to the selected archive and license SHA-256. A refresh, reconnect or endpoint change invalidates local review; changed server hashes require a new review instead of installing a different approved revision.
- Audition tail budgets add the enabled effects' explicit `tail_seconds` along each routed path and take the longest parallel path to the master. Bypassed/disconnected effects do not extend the budget; successful live tail edits recalculate the stopping point. This does not detect a plugin's full decay or compensate plugin latency.
- While stopped, audition blocks can be 512/1024/2048/4096 frames with 2 or 4 prebuffer blocks; the default remains 2048/4. These session-only settings apply to native VST processing and soundcard output, not ordinary browser playback without VST. Smaller blocks increase HTTP/request pressure and may underrun or stop explicitly on sustained overload; increase the block size/prebuffer if unstable. Displayed block budgets exclude device-driver, capture and network overhead, and are not end-to-end latency measurements.
- Windows shutdown acceptance must close the PID-owned `Tauri Window`, not the visible `Tao Thread Event Target` helper. The earlier process-residue report resulted from closing that helper. The unchanged installer exits normally when closing the main window, including during native VST playback. Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/smoke_desktop_shutdown.ps1 -Executable <installed-path>/svs-desktop.exe` to verify idle shutdown, owned-process exit and backend port release; it refuses occupied backend ports before launch. No product shutdown patch or installer rebuild was needed.
- Offline VST rendering removes reported sample delay and drains the signal end, accounting for startup samples already withheld by the host. Continuous audition aligns track outputs/sends, nested buses and the master using a fixed session delay budget. Bypass preserves that budget and keeps the plugin/dry delay running; latency-changing parameters stop the session and require restarting playback. The transport displays plugin compensation separately from network buffering and delays its audible clock until startup has passed. Unreported or inaccurate plugin latency cannot be corrected. These compensation changes are included in the current locally verified Windows installer.
- Tools → VST3 retains its standalone editor. Mixer → live instance editor embeds the owned worker's native JUCE window inside Windows Tauri; browsers retain the separate native window. Exact process/session/revision/window ownership is checked. Close reads parameters and the native preset; explicit Apply stops audition and commits against the original project revision, rejecting intervening edits. Presets persist in projects/bundles, restore before parameter overrides and are limited to 4 MiB; external files and DSP history are excluded. Nova child-window parenting, close readback and explicit preset persistence were exercised. The installer remains unsigned and is not cleared for public redistribution.
- Late editor opens are closed after leaving the mixer; resize/detach require the full session/worker/revision identity. Tauri embeds the owned JUCE content child, not Pedalboard's top-level host: the host remains visible offscreen so its editor loop stays alive. Native regions clip content to the viewport and scrolling ancestors; empty regions preserve the editor while fully clipped. Detach restores the original parent, region and geometry. Frozen workers declare PerMonitorV2 and initialize process/thread DPI contexts. Repeated Nova attach/clip/restore/close, including minimized attachment, was exercised locally; other DPI setups and arbitrary plugins remain unverified. Missing window discovery fails after five seconds; closing interrupts discovery promptly.
- Resize/detach also validate the current content parent and original host owner; a closed editor releases its saved region and attachment state so the next editor can open. The six local editors (Nova, WET and four Dragonfly variants) were exercised twice each with empty clipping, restoration, reopen, state readback and continued processing; this is not arbitrary-plugin or cross-DPI certification.
- WET Reverb 1.3.0 and Dragonfly 3.2.10 Early Reflections/Hall/Plate/Room were exercised with saved presets plus parameter overrides, offline track/bus/master processing at 24/44.1/48 kHz, and continuous sessions. Presets alone are not sufficient: WET did not restore its selected mode, and Dragonfly parameter writes were not reflected in captured state until processing. Keep the preset and parameters together. Native editor lifecycle acceptance now covers these local variants, but plugin-specific visual editing, arbitrary plugins and other machines remain outside that claim.
- Track and bus send levels can change during native audition without restarting the session, including changes saved through the mixer UI. Adding/removing sends or changing destinations still requires restarting playback.
- Native audition prepares phrases for muted and non-solo tracks too, so unmuting or releasing solo can restore the current phrase without restarting playback. These phrases can increase initial synthesis wait and consume the existing bounded cache; mute/solo changes are heard after already-buffered output.
- Live native track pan applies to both active sources and later phrases/audio clips in the same session; it does not reset when the next source starts. Changes are heard after already-buffered output.
- Native PCM networking runs in a dedicated Web Worker connected directly to the AudioWorklet, rather than the renderer thread. Audio-clock messages keep native scheduling active without animation frames. Queues remain bounded; underruns refill in order, while sustained overload stops explicitly. This does not guarantee uninterrupted playback during system sleep or indefinite renderer stalls.
- Capture uses seven preallocated PCM buffers transferred between AudioWorklet and stream worker, recycled only after the HTTP block and optional device submission finish. Recycled blocks are zeroed before reuse, including absent tracks. A stalled worker exhausts this fixed pool and stops explicitly instead of allocating indefinitely or silently dropping capture.
- Tools → Project sync supports manual WebDAV bundle upload and restore on Windows. Supply an existing HTTPS collection URL and an application password; credentials are protected by current-user Windows DPAPI. Conditional ETags prevent blind overwrites and preserve conflicting uploads under separate names. Restore requires a new directory, backs up the current project and media, and refuses to replace a project edited during download. Real authenticated loopback WebDAV was exercised; no hosted Nextcloud deployment or cross-machine cloud recovery is claimed. This is not automatic bidirectional merging.
- WebDAV listings honor each property's status and XML namespace; unavailable properties and same-name extension fields do not overwrite successful DAV metadata. This compatibility fix does not establish public-server or cross-machine acceptance.
- Tools → score import/export supports SVS project bundles (`.svsbundle`): a project snapshot plus referenced audio, deduplicated and checked against a SHA-256 manifest. Relative source audio requires an explicit project base directory; restoration requires a new directory and refuses overwrites. Voicebanks, models, plugins and credentials are not included. This is portable backup/restore, not cloud synchronization.
- For relative media paths, provide the project base directory before sync. Local pre-restore bundles remain in `%APPDATA%/SVS Studio/project-sync/backups/`; disconnect removes the saved profile/credential, not these backups. Voicebanks, models and plugins are not uploaded. Secure credential persistence currently requires Windows; other platforms refuse rather than store plaintext.
- AI composition/accompaniment depends on an externally configured chat/model service, not a bundled local LLM.
- GPU model inference requires external compatible dependencies and assets; not every audio operation runs on GPU.

## Licensing and third-party assets

SVS Studio's original source code and documentation are licensed under the [Apache License, Version 2.0](LICENSE) (SPDX: `Apache-2.0`). Copyright 2026 Brian98Z and SVS Studio contributors. Third-party dependencies, models, plugins, voicebanks, and external services retain their own licenses and terms; this project's license does not relicense them or grant rights to their assets. Verify their terms before use or redistribution.
