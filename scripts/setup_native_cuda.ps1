[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvironmentDirectory,
    [string]$PythonExecutable = 'python'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$environmentPath = [IO.Path]::GetFullPath($EnvironmentDirectory)
if (Test-Path -LiteralPath $environmentPath) {
    throw "Use a new environment directory; refusing to modify $environmentPath"
}
& $PythonExecutable -c "import sys,struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3,11) and struct.calcsize('P') == 8, 'Requires Windows x64 Python 3.11'"
if ($LASTEXITCODE -ne 0) { throw 'Interpreter preflight failed' }
$wheel = Join-Path $root 'build/native-cuda/wheels/diffsinger_utau-0.3.8+svscuda1-py3-none-any.whl'
if (-not (Test-Path -LiteralPath $wheel)) {
    & $PythonExecutable (Join-Path $PSScriptRoot 'build_native_cuda.py')
    if ($LASTEXITCODE -ne 0) { throw 'CUDA dependency wheel build failed (requires uv on PATH)' }
}
& $PythonExecutable -m venv $environmentPath
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
$python = Join-Path $environmentPath 'Scripts/python.exe'
Push-Location $root
try {
    & $python -m pip --isolated install --index-url https://pypi.org/simple -r requirements-native-gpu.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed; incomplete environment retained at $environmentPath" }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Installed dependency check failed' }
} finally { Pop-Location }
Write-Output "Native CUDA Python: $python"
Write-Output 'Set SVS_GPU_PYTHON to this executable before starting SVS Studio. Configure the external ABC integration, voicebank and independently licensed vocoder separately.'
