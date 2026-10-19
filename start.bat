@echo off
title SVS Studio Server
cd /d "%~dp0"
rem Reuse an already-running instance: just open the browser
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8761/api/health' -TimeoutSec 2 -UseBasicParsing; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
  echo [SVS Studio] server already running - opening browser
  start "" http://127.0.0.1:8761/
  timeout /t 2 >nul
  exit /b 0
)
echo [SVS Studio] starting server
target\release\svs-server.exe
set EC=%errorlevel%
if not "%EC%"=="0" (
  echo.
  echo [SVS Studio] server exited with error code %EC%
  echo Common cause: port 8761 already in use - another instance may be running
  echo Press any key to close this window
  pause >nul
)
