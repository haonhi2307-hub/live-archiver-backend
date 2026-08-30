@echo off
setlocal
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo Creating Python virtual environment...
  python -m venv .venv || exit /b 1
)

echo [1/3] Installing Python quality tools...
.venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1

echo [2/3] Installing Playwright Chromium for official-player observation...
.venv\Scripts\python.exe -m playwright install chromium || (
  echo [WARN] Playwright browser install failed. API/yt-dlp resolver will still work,
  echo        but official-player observation will be unavailable until this succeeds.
)

echo [3/3] Checking ffprobe...
where ffprobe >nul 2>&1
if %errorlevel%==0 (
  echo [OK] ffprobe is already available.
  ffprobe -version | findstr /B /C:"ffprobe version"
  goto :done
)
where winget >nul 2>&1
if not %errorlevel%==0 (
  echo [WARN] winget not found. Install FFmpeg manually and ensure ffprobe.exe is in PATH.
  goto :done
)
winget install -e --id Gyan.FFmpeg --scope user --accept-package-agreements --accept-source-agreements

echo.
echo Close and reopen Android Studio/PowerShell if FFmpeg was just installed.
:done
echo Quality tools setup complete.
