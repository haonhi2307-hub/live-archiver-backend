@echo off
setlocal
if "%LIVE_ARCHIVER_BACKEND_PORT%"=="" set LIVE_ARCHIVER_BACKEND_PORT=8001
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python virtual environment...
  python -m venv .venv || exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
where ffprobe >nul 2>&1
if errorlevel 1 (
  echo [WARN] ffprobe not found - VERIFIED MAX quality probing is reduced.
) else (
  echo [OK] ffprobe detected.
)
.venv\Scripts\python.exe -c "from app.browser_observer import available; print('[Official player observer]', 'python-ready' if available() else 'not-installed')"
.venv\Scripts\python.exe -c "from app.settings import settings; print('[TikTok auth]', ('browser:'+settings.tiktok_browser) if settings.tiktok_browser else ('sessionid' if settings.tiktok_sessionid else ('cookie_header' if settings.tiktok_cookies else 'anonymous')))"
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port %LIVE_ARCHIVER_BACKEND_PORT%
