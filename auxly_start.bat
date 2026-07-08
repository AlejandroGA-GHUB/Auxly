@echo off
title Auxly Music Bot
cd /d "%~dp0"

rem Local version only (line 1 of VERSION.txt). Whether an update exists is
rem the bot's job: it checks GitHub once at startup and on a!version.
set "AUXLY_VER=unknown"
if exist VERSION.txt set /p AUXLY_VER=<VERSION.txt
echo Auxly v%AUXLY_VER%
echo.

echo Checking for yt-dlp updates...
set "YTDLP_OLD="
for /f "delims=" %%v in ('python -c "import yt_dlp; print(yt_dlp.version.__version__)" 2^>nul') do set "YTDLP_OLD=%%v"
python -m pip install -U yt-dlp yt-dlp-ejs --quiet --disable-pip-version-check
set "YTDLP_NEW="
for /f "delims=" %%v in ('python -c "import yt_dlp; print(yt_dlp.version.__version__)" 2^>nul') do set "YTDLP_NEW=%%v"
if "%YTDLP_OLD%"=="%YTDLP_NEW%" (
    echo yt-dlp is up to date - %YTDLP_NEW%
) else (
    echo yt-dlp updated: %YTDLP_OLD% to %YTDLP_NEW%
)
echo.

set "DB=0.0"
set "AF=0.0"
for /f "tokens=1,2 delims==" %%a in ('powershell -NoProfile -Command "$db=0; if(Test-Path auxly.db){$db=(Get-Item auxly.db).Length}; $af=0; if(Test-Path audio_files){foreach($f in (Get-ChildItem audio_files -Recurse -File)){$af+=$f.Length}}; 'DB={0:N2}' -f ($db/1MB); 'AF={0:N2}' -f ($af/1MB)"') do set "%%a=%%b"
echo Local storage used:
echo    Database (auxly.db): %DB% MB
echo    Stored audio files (audio_files): %AF% MB
echo.

set "BOTPID="
for /f "usebackq" %%p in (`powershell -NoProfile -Command "(Start-Process pythonw -ArgumentList 'bot.py' -PassThru).Id"`) do set "BOTPID=%%p"
echo Auxly is starting in the background (output goes to auxly.log).
echo.
echo Command help: a!help ^| a!profilehelp ^| a!devhelp (owner)
echo.
echo    ESC     Close this window; Auxly keeps running windowless.
echo            Stop it later with a!shutdown in Discord.
echo    Ctrl+C  Shut Auxly down now and close this window.
echo.
powershell -NoProfile -Command "try{[Console]::TreatControlCAsInput=$true}catch{}; while($true){try{$k=[Console]::ReadKey($true)}catch{exit 0}; if($k.Key -eq 'Escape'){exit 0}; if($k.Key -eq 'C' -and ($k.Modifiers -band [ConsoleModifiers]::Control)){exit 1}}"
if errorlevel 1 (
    taskkill /pid %BOTPID% /t /f >nul 2>&1
    echo Auxly stopped.
    timeout /t 2 /nobreak >nul
)
