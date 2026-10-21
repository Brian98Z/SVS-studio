[CmdletBinding()]
param(
    [ValidateSet('windows-x64')]
    [string]$Target = 'windows-x64',
    [string]$ModelDirectory,
    [string]$VoicebankDirectory,
    [string]$BuildPython,
    [string]$Wheelhouse,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$desktop = Join-Path $root 'apps/desktop'
$tauri = Join-Path $desktop 'src-tauri'
if (-not $BuildPython) { $BuildPython = Join-Path $tauri '.venv-release-lock/Scripts/python.exe' }
if (-not $Wheelhouse) { $Wheelhouse = Join-Path $root 'release/python/wheelhouse' }
$runtime = Join-Path $tauri 'runtime'
$resourceSource = Join-Path $root 'packages/inference-service'
$serverExe = 'svs-server.exe'
$buildTarget = Join-Path $env:TEMP 'svs-desktop-build-target'
$serverPath = Join-Path $buildTarget "release/$serverExe"

function Require-Command([string]$Name, [string]$Purpose) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Missing prerequisite '$Name' ($Purpose)." }
}
function Require-File([string]$Path, [string]$Purpose) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing $Purpose at '$Path'." }
}
function Copy-ReleaseTree([string]$Source, [string]$Destination) {
    Get-ChildItem -LiteralPath $Source -Recurse -File -Force | Where-Object {
        $_.Name -notlike 'test_*.py' -and ($_.DirectoryName -split '[\\/]') -notcontains '__pycache__'
    } | ForEach-Object {
        $relative = $_.FullName.Substring($Source.Length).TrimStart([char[]]@('/', '\'))
        $target = Join-Path $Destination $relative
        $parent = Split-Path -Parent $target
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}


Require-Command 'cargo' 'Rust server build'
Require-Command 'npm' 'Tauri desktop build'
Require-File (Join-Path $desktop 'package-lock.json') 'desktop npm lockfile'
Require-File (Join-Path $root 'Cargo.lock') 'Cargo lockfile'
Require-File (Join-Path $resourceSource 'inference_service/__init__.py') 'inference service package source'
Require-File (Join-Path $resourceSource 'inference_service/service_runner.py') 'inference service entry point'
Require-File (Join-Path $PSScriptRoot 'inference.manifest') 'native inference DPI manifest'
Require-File (Join-Path $root 'packages/g2p-engine/g2p.py') 'G2P engine source'
Require-File (Join-Path $root 'packages/interchange/bridge.py') 'interchange bridge source'
Require-File (Join-Path $root 'packages/interchange/project_bundle.py') 'interchange bundle source'
if ($ModelDirectory) {
    $ModelDirectory = (Resolve-Path -LiteralPath $ModelDirectory).Path
    foreach ($name in @('dur_model.onnx', 'pitch_model.onnx', 'acoustic_model.onnx', 'vocoder.onnx')) {
        Require-File (Join-Path $ModelDirectory $name) "selected ONNX model $name"
    }
}
if ($VoicebankDirectory) { $VoicebankDirectory = (Resolve-Path -LiteralPath $VoicebankDirectory).Path }
Require-File (Join-Path $root 'release/python/THIRD-PARTY-NOTICE.txt') 'third-party Python notice'
Require-File (Join-Path $root 'release/python/third-party/THIRD-PARTY-INVENTORY.json') 'third-party Python inventory'
Require-File (Join-Path $root 'release/python/pedalboard-submodules/SUBMODULE-INVENTORY.json') 'Pedalboard submodule inventory'
Require-File (Join-Path $root 'release/python/pedalboard-submodules/SUBMODULE-LICENSES.json') 'Pedalboard submodule license inventory'
Require-File (Join-Path $root 'release/python/PEDALBOARD-SOURCE-STATUS.json') 'Pedalboard source status'
foreach ($archive in @('pedalboard-0.9.26-source.tar.gz', 'pedalboard-submodules/JUCE.tar.gz', 'pedalboard-submodules/rubberband.tar.gz', 'pedalboard-submodules/lame.tar.gz', 'pedalboard-submodules/libgsm.tar.gz', 'pedalboard-submodules/pybind11.tar.gz')) {
    Require-File (Join-Path $root "release/python/$archive") "release Python source archive $archive"
}
if (-not (Test-Path -LiteralPath (Join-Path $root 'release/python/third-party') -PathType Container)) { throw 'Missing third-party Python license inputs directory.' }
if (-not (Test-Path -LiteralPath (Join-Path $root 'release/python/pedalboard-submodules') -PathType Container)) { throw 'Missing Pedalboard submodule license inputs directory.' }
if (-not $PrepareOnly) {
    if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) { throw "Isolated Python runtime missing: $BuildPython" }
    & $BuildPython (Join-Path $root 'scripts/release_python.py') verify --python $BuildPython --wheelhouse $Wheelhouse
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Python environment differs from the release hash lock.' }
    & $BuildPython -c "import PyInstaller; import flask, onnxruntime, yaml, librosa, pedalboard, waitress"
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Python runtime lacks the locked desktop inference dependencies.' }
}




Write-Host "Target: $Target (Windows x64 native)"
if ($ModelDirectory) { Write-Host "Explicit ONNX model directory: $ModelDirectory" } else { Write-Host 'No ONNX models bundled; DSP synthesis remains available.' }
if ($VoicebankDirectory) { Write-Host "Explicit voicebank directory: $VoicebankDirectory (operator-selected; verify applicable license)" } else { Write-Host 'No voicebank bundled.' }
if ($PrepareOnly) { Write-Host 'Prerequisite inspection complete; no build performed.'; exit 0 }

$wrapper = Join-Path $env:TEMP 'svs_inference_entry.py'
@'
import multiprocessing

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from inference_service.service_runner import main
    main()
'@ | Set-Content -LiteralPath $wrapper -Encoding utf8
try {
    Push-Location $root
    try {
        $env:CARGO_TARGET_DIR = $buildTarget
        cargo build --release --locked -p svs-server
        if ($LASTEXITCODE -ne 0) { throw "Cargo server build failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
    Push-Location $desktop
    try {
        & (Join-Path $desktop 'node_modules/.bin/vite.cmd') build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
    Require-File (Join-Path $desktop 'dist/index.html') 'built desktop frontend dist/index.html'


    Require-File $serverPath 'built Rust server sidecar'
    if (Test-Path -LiteralPath $runtime) { Remove-Item -LiteralPath $runtime -Recurse -Force }
    New-Item -ItemType Directory -Path $runtime | Out-Null
    Copy-Item -LiteralPath $serverPath -Destination (Join-Path $runtime 'svs-server.exe')
    $inferenceBundle = Join-Path $runtime 'inference-service'
    $g2pBundle = Join-Path $runtime 'g2p-engine'
    New-Item -ItemType Directory -Path $inferenceBundle | Out-Null
    Copy-ReleaseTree (Join-Path $resourceSource 'inference_service') (Join-Path $inferenceBundle 'inference_service')
    Copy-ReleaseTree (Join-Path $root 'packages/g2p-engine') $g2pBundle
    Copy-ReleaseTree (Join-Path $desktop 'dist') (Join-Path $runtime 'desktop-dist')
    if ($ModelDirectory) { Copy-ReleaseTree $ModelDirectory (Join-Path $inferenceBundle 'models') }
    if ($VoicebankDirectory) { Copy-ReleaseTree $VoicebankDirectory (Join-Path $runtime 'voicebank') }
    $licenseBundle = Join-Path $runtime 'licenses/python'
    New-Item -ItemType Directory -Path $licenseBundle | Out-Null
    Copy-ReleaseTree (Join-Path $root 'release/python/third-party') $licenseBundle
    Get-ChildItem -LiteralPath (Join-Path $root 'release/python') -File | Where-Object { $_.Name -like 'PEDALBOARD-*' -or $_.Name -eq 'THIRD-PARTY-NOTICE.txt' } | Copy-Item -Destination $licenseBundle -Force
    Copy-ReleaseTree (Join-Path $root 'release/python/pedalboard-submodules') (Join-Path $licenseBundle 'pedalboard-submodules')
    Copy-Item -LiteralPath (Join-Path $root 'release/python/third-party/THIRD-PARTY-INVENTORY.json') -Destination $licenseBundle -Force
    Require-File (Join-Path $licenseBundle 'THIRD-PARTY-INVENTORY.json') 'third-party Python inventory'
    $interchangeBundle = Join-Path $runtime 'interchange'
    New-Item -ItemType Directory -Path $interchangeBundle | Out-Null
    Copy-Item -LiteralPath (Join-Path $root 'packages/interchange/bridge.py') -Destination $interchangeBundle
    Copy-Item -LiteralPath (Join-Path $root 'packages/interchange/project_bundle.py') -Destination $interchangeBundle

    $pythonBundle = Join-Path $runtime 'python'
    New-Item -ItemType Directory -Path $pythonBundle | Out-Null
    if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) {
        throw "Isolated Python environment missing at $BuildPython. Provision it from requirements-build.txt and scripts/release-windows-py311.lock."
    }
    & $BuildPython -m PyInstaller --noconfirm --clean --onedir --noconsole --name svs-python `
        --manifest (Join-Path $PSScriptRoot 'inference.manifest') `
        --distpath $pythonBundle --workpath (Join-Path $env:TEMP 'svs-pyinstaller-work') `
        --paths $resourceSource --collect-all flask --collect-all numpy --collect-all scipy `
        --collect-all soundfile --collect-all onnxruntime --collect-all yaml --collect-all librosa `
        --collect-all pedalboard --collect-all waitress $wrapper
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller inference bundle failed with exit code $LASTEXITCODE." }

    Push-Location $desktop
    try {
        & (Join-Path $desktop 'node_modules/.bin/tauri.cmd') build --bundles nsis
        if ($LASTEXITCODE -ne 0) { throw "Tauri packaging failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
} finally {
    Remove-Item -LiteralPath $wrapper -Force -ErrorAction SilentlyContinue
}
