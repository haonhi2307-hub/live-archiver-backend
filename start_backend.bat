@echo off
setlocal
if "%LIVE_ARCHIVER_BACKEND_PORT%"=="" set LIVE_ARCHIVER_BACKEND_PORT=8001
if not exist .venv\Scripts\python.exe (
  python -m venv .venv
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
where ffprobe >nul 2>&1
if errorlevel 1 (
  echo [INFO] ffprobe not found. Resolver will still use TikTok/yt-dlp metadata, but quality verification is disabled.
  echo [INFO] Optional: run install_quality_tools_windows.bat once, then reopen the terminal.
) else (
  echo [OK] ffprobe detected - actual resolution/fps/codec verification enabled.
)
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port %LIVE_ARCHIVER_BACKEND_PORT%
