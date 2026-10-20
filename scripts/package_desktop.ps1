[CmdletBinding()]
param(
    [ValidateSet('windows-x64')]
    [string]$Target = 'windows-x64',
    [string]$ModelDirectory,
    [string]$VoicebankDirectory,
    [string]$PythonExecutable,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$desktop = Join-Path $root 'apps/desktop'
$tauri = Join-Path $desktop 'src-tauri'
$runtime = Join-Path $tauri 'runtime'
 $resourceSource = Join-Path $root 'packages/inference-service'
 $manifest = Join-Path $root 'requirements-build.txt'
$serverExe = 'svs-server.exe'
$buildTarget = Join-Path $env:TEMP 'svs-desktop-build-target'
$serverPath = Join-Path $buildTarget "release/$serverExe"

function Require-Command([string]$Name, [string]$Purpose) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) { throw "Missing prerequisite '$Name' ($Purpose)." }
}
function Require-File([string]$Path, [string]$Purpose) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Missing $Purpose at '$Path'." }
}

Require-Command 'cargo' 'Rust server build'
Require-Command 'npm' 'Tauri desktop build'
Require-File (Join-Path $desktop 'package-lock.json') 'desktop npm lockfile'
Require-File (Join-Path $root 'Cargo.lock') 'Cargo lockfile'
Require-File $manifest 'desktop inference dependency manifest'
if (-not (Test-Path -LiteralPath (Join-Path $desktop 'node_modules/.bin/tauri.cmd'))) {
    throw 'Local Tauri CLI missing. Run npm ci in apps/desktop with the locked package-lock.json.'
}
if (-not $PrepareOnly) {
    $isolatedPython = Join-Path $tauri '.venv-build/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $isolatedPython -PathType Leaf)) { throw "Isolated Python runtime missing: $isolatedPython" }
    & $isolatedPython -c "import PyInstaller; import flask, onnxruntime, yaml, librosa, pedalboard"
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Python runtime lacks the locked desktop inference dependencies.' }
}
if ($ModelDirectory) {
    $ModelDirectory = (Resolve-Path -LiteralPath $ModelDirectory).Path
    foreach ($name in @('dur_model.onnx', 'pitch_model.onnx', 'acoustic_model.onnx', 'vocoder.onnx')) {
        Require-File (Join-Path $ModelDirectory $name) "selected ONNX model $name"
    }
}

Write-Host "Target: $Target (Windows x64 native)"
Write-Host "Python: $PythonExecutable"
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
    Require-File (Join-Path $desktop 'dist/index.html') 'built desktop frontend dist/index.html'


    Require-File $serverPath 'built Rust server sidecar'
    if (Test-Path -LiteralPath $runtime) { Remove-Item -LiteralPath $runtime -Recurse -Force }
    New-Item -ItemType Directory -Path $runtime | Out-Null
    Copy-Item -LiteralPath $serverPath -Destination (Join-Path $runtime 'svs-server.exe')
    $inferenceBundle = Join-Path $runtime 'inference-service'
    New-Item -ItemType Directory -Path $inferenceBundle | Out-Null
    Copy-Item -LiteralPath (Join-Path $resourceSource 'inference_service') -Destination $inferenceBundle -Recurse
    $g2pBundle = Join-Path $runtime 'g2p-engine'
    Copy-Item -LiteralPath (Join-Path $root 'packages/g2p-engine') -Destination $g2pBundle -Recurse -Exclude '__pycache__', 'test_*.py'
    $interchangeBundle = Join-Path $runtime 'interchange'
    New-Item -ItemType Directory -Path $interchangeBundle | Out-Null
    Copy-Item -LiteralPath (Join-Path $root 'packages/interchange/bridge.py') -Destination $interchangeBundle
    Copy-Item -LiteralPath (Join-Path $root 'packages/interchange/project_bundle.py') -Destination $interchangeBundle
    Copy-Item -LiteralPath (Join-Path $desktop 'dist') -Destination (Join-Path $runtime 'desktop-dist') -Recurse
    if ($ModelDirectory) { Copy-Item -LiteralPath $ModelDirectory -Destination (Join-Path $inferenceBundle 'models') -Recurse }
    if ($VoicebankDirectory) { Copy-Item -LiteralPath $VoicebankDirectory -Destination (Join-Path $runtime 'voicebank') -Recurse }

    $pythonBundle = Join-Path $runtime 'python'
    New-Item -ItemType Directory -Path $pythonBundle | Out-Null
    if (-not (Test-Path -LiteralPath $isolatedPython -PathType Leaf)) {
        throw "Isolated Python environment missing at $isolatedPython. Provision it with the repository-root requirements-build.txt plus PyInstaller."
    }
    & $isolatedPython -m PyInstaller --noconfirm --clean --onedir --noconsole --name svs-python `
        --distpath $pythonBundle --workpath (Join-Path $env:TEMP 'svs-pyinstaller-work') `
        --paths $resourceSource --collect-all flask --collect-all numpy --collect-all scipy `
        --collect-all soundfile --collect-all onnxruntime --collect-all yaml --collect-all librosa `
        --collect-all pedalboard $wrapper
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller inference bundle failed with exit code $LASTEXITCODE." }

    Push-Location $desktop
    try {
        & (Join-Path $desktop 'node_modules/.bin/tauri.cmd') build --bundles nsis
        if ($LASTEXITCODE -ne 0) { throw "Tauri packaging failed with exit code $LASTEXITCODE." }
    } finally { Pop-Location }
} finally {
    Remove-Item -LiteralPath $wrapper -Force -ErrorAction SilentlyContinue
}
