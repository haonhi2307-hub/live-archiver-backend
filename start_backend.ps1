$port = if ($env:LIVE_ARCHIVER_BACKEND_PORT) { $env:LIVE_ARCHIVER_BACKEND_PORT } else { "8001" }
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
}
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
if (Get-Command ffprobe -ErrorAction SilentlyContinue) {
    Write-Host "[OK] ffprobe detected - actual quality verification enabled." -ForegroundColor Green
} else {
    Write-Host "[INFO] ffprobe not found. Metadata quality still works; run install_quality_tools_windows.bat for verified probing." -ForegroundColor Yellow
}
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port $port
