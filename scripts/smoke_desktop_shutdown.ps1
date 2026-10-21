[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'
$executablePath = (Resolve-Path -LiteralPath $Executable).Path
if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) { throw 'Executable must be a file.' }

if (-not ('SvsShutdownWindows' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class SvsShutdownWindows {
    private delegate bool EnumProc(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc callback, IntPtr parameter);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder name, int capacity);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessage(IntPtr window, uint message, IntPtr wParam, IntPtr lParam);
    public static IntPtr[] MainWindows(uint process) {
        var windows = new List<IntPtr>();
        EnumWindows((window, parameter) => {
            uint owner;
            GetWindowThreadProcessId(window, out owner);
            var name = new StringBuilder(256);
            GetClassName(window, name, name.Capacity);
            // Tao's visible thread-event helper is not an application window.
            if (owner == process && IsWindowVisible(window) && name.ToString() == "Tauri Window") windows.Add(window);
            return true;
        }, IntPtr.Zero);
        return windows.ToArray();
    }
    public static bool Close(IntPtr window) { return PostMessage(window, 0x0010, IntPtr.Zero, IntPtr.Zero); }
}
'@
}

foreach ($port in @(8760, 8761)) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        if ($client.ConnectAsync('127.0.0.1', $port).Wait(250) -and $client.Connected) {
            throw "Port $port is already in use; refusing to inspect an unmanaged server."
        }
    } catch [System.AggregateException] {
        # Connection refusal means no existing service owns the port.
    } finally { $client.Dispose() }
}

$app = Start-Process -FilePath $executablePath -WorkingDirectory (Split-Path $executablePath) -PassThru
$owned = @($app)
try {
    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        if ($app.HasExited) { throw "Desktop exited before readiness: $($app.ExitCode)" }
        $windows = @([SvsShutdownWindows]::MainWindows($app.Id))
        if ($windows.Count -eq 1) {
            try {
                $null = Invoke-RestMethod -Uri 'http://127.0.0.1:8761/api/project' -TimeoutSec 1
                $ready = $true
            } catch { }
        }
        if (-not $ready) { Start-Sleep -Milliseconds 100 }
    } until ($ready -or [DateTime]::UtcNow -ge $deadline)
    if (-not $ready) { throw 'One Tauri main window and a ready API were not observed within 30 seconds.' }

    $snapshot = @(Get-CimInstance Win32_Process)
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    $null = $ids.Add($app.Id)
    do {
        $added = $false
        foreach ($entry in $snapshot) {
            if ($ids.Contains([int]$entry.ParentProcessId) -and $ids.Add([int]$entry.ProcessId)) {
                $added = $true
                try { $owned += [System.Diagnostics.Process]::GetProcessById([int]$entry.ProcessId) } catch [System.ArgumentException] { }
            }
        }
    } while ($added)
    # Force opening handles before closing; these identify the original processes even if PIDs are reused.
    foreach ($process in $owned) { try { $null = $process.Handle } catch [System.InvalidOperationException] { } }
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    if (-not [SvsShutdownWindows]::Close($windows[0])) { throw 'Could not post WM_CLOSE to the Tauri main window.' }
    foreach ($process in $owned) {
        $remaining = [Math]::Max(0, $TimeoutSeconds * 1000 - [int]$timer.ElapsedMilliseconds)
        if (-not $process.WaitForExit($remaining)) { throw "Owned process $($process.Id) remained after closing the main window." }
    }
    if ($app.ExitCode -ne 0) { throw "Desktop exit code: $($app.ExitCode)" }
    foreach ($port in @(8760, 8761)) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            if ($client.ConnectAsync('127.0.0.1', $port).Wait(250) -and $client.Connected) { throw "Port $port remains open." }
        } catch [System.AggregateException] {
        } finally { $client.Dispose() }
    }
    [ordered]@{
        executable = $executablePath
        closed_window_class = 'Tauri Window'
        exit_code = $app.ExitCode
        elapsed_ms = $timer.ElapsedMilliseconds
        owned_process_count = $owned.Count
        owned_survivors = 0
        ports_closed = @(8760, 8761)
    } | ConvertTo-Json
} finally {
    if (-not $app.HasExited) {
        # Test-failure cleanup only; never counted as successful shutdown.
        & taskkill.exe /PID $app.Id /T /F | Out-Null
    }
    foreach ($process in $owned) { $process.Dispose() }
}
