@echo off
REM -----------------------------------------------------------------------
REM   Briefa - Launcher (run.bat)
REM   - Add bin\ffmpeg\bin vao PATH (neu co ffmpeg portable do SETUP.bat tai)
REM   - Activate venv qua run.ps1
REM   - Mo menu interactive: chon kenh, chon mode (text/url/image), render
REM
REM   Lan dau su dung: chay SETUP.bat truoc.
REM -----------------------------------------------------------------------

cd /d "%~dp0"

REM Force UTF-8 console code page so Vietnamese channel display names
REM (Khue Tran -> "Khue^ Tra^n") render correctly trong PowerShell.
chcp 65001 > nul

if exist "%~dp0bin\ffmpeg\bin\ffmpeg.exe" (
    set "PATH=%~dp0bin\ffmpeg\bin;%PATH%"
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [X] Chua co .venv - ban chua chay SETUP.bat?
    echo     Double-click SETUP.bat truoc roi moi chay run.bat.
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
