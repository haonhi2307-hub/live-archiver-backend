@echo off
setlocal
echo ===================================================
echo Live Archiver - TikTok Chrome Auth Session Setup
echo ===================================================
set "PROFILE_DIR=%~dp0data\browser_profile"
if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"
echo Profile Directory: %PROFILE_DIR%
echo Please log in to TikTok in the opened Chrome window.
echo Once logged in, close the browser completely.
start "" "chrome.exe" --user-data-dir="%PROFILE_DIR%" "https://www.tiktok.com/login"
echo Auth setup completed.
