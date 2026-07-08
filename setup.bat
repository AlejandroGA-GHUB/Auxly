@echo off
title Auxly Setup
cd /d "%~dp0"

echo ================================================
echo  Auxly one-time setup
echo ================================================
echo.

rem Create the settings file FIRST - it needs nothing installed, and doing
rem it here means an early exit below (fresh Python install, pip hiccup)
rem can never leave the user without a .env.
if not exist .env.example if not exist .env (
    echo ERROR: .env.example is missing. Make sure you downloaded or cloned
    echo the WHOLE project folder and are running setup.bat from inside it.
    pause
    exit /b 1
)
if not exist .env (
    copy .env.example .env >nul
    echo Created .env - the settings file your bot token goes into.
    echo.
)

rem What's already here? (the Microsoft Store python stub exits nonzero,
rem so a plain version check correctly treats it as missing)
set "HAVE_PYTHON=1"
set "HAVE_FFMPEG=1"
set "HAVE_DENO=1"
python --version >nul 2>&1 || set "HAVE_PYTHON="
ffmpeg -version >nul 2>&1 || set "HAVE_FFMPEG="
deno --version >nul 2>&1 || set "HAVE_DENO="

if defined HAVE_PYTHON if defined HAVE_FFMPEG if defined HAVE_DENO goto :deps

set "HAVE_WINGET=1"
winget --version >nul 2>&1 || set "HAVE_WINGET="

if not defined HAVE_WINGET (
    echo This script uses winget to install missing programs, but winget
    echo was not found on this PC. Install these manually instead:
    echo.
    if not defined HAVE_PYTHON echo    Python 3.11+ - https://www.python.org/downloads/
    if not defined HAVE_FFMPEG echo    FFmpeg - https://ffmpeg.org/download.html - must be on PATH
    if not defined HAVE_DENO echo    Deno - https://deno.com - yt-dlp uses it for smooth YouTube audio
    echo.
)
rem Deno is recommended, not required - only Python/FFmpeg block the setup.
if not defined HAVE_WINGET if defined HAVE_PYTHON if defined HAVE_FFMPEG (
    echo Deno can be added any time - continuing setup without it.
    echo.
    goto :deps
)
if not defined HAVE_WINGET (
    echo Then double-click setup.bat again.
    pause
    exit /b 1
)

if not defined HAVE_PYTHON (
    echo Installing Python - this takes a minute...
    winget install -e --id Python.Python.3.13 --accept-source-agreements --accept-package-agreements
)
if not defined HAVE_FFMPEG (
    echo Installing FFmpeg...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)
if not defined HAVE_DENO (
    echo Installing Deno - yt-dlp uses it for smooth YouTube audio...
    winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
)

rem A freshly installed Python isn't visible to THIS window (PATH is read
rem at window start). A second run picks it up and finishes the job.
if not defined HAVE_PYTHON (
    echo.
    echo Python is installed, but this window can't see it yet.
    echo Close this window and double-click setup.bat once more to finish.
    pause
    exit /b 0
)

:deps
echo.
echo Installing the bot's Python packages...
python -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo Package install failed - check the messages above, then run
    echo setup.bat again.
    pause
    exit /b 1
)

echo.
echo ================================================
echo  Almost done - Auxly needs its Discord token
echo ================================================
echo.
echo 1. Create your bot at https://discord.com/developers/applications
echo    and copy its token - full walkthrough in README.md, step
echo    "Create the Discord bot".
echo 2. Paste the token into the .env file, which opens in Notepad
echo    when you press a key below:  DISCORD_TOKEN=your-token-here
echo 3. Save, close Notepad, then double-click auxly_start.bat to
echo    launch Auxly.
echo.
echo Press any key to open .env in Notepad...
pause >nul
start "" notepad .env
exit /b 0
