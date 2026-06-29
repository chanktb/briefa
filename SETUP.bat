@echo off
REM -----------------------------------------------------------------------
REM   Briefa - Setup (wraps SETUP.ps1)
REM   Double-click file nay - tool kiem tra Python/Node/ffmpeg + cai deps.
REM   Logic that nam o SETUP.ps1 (PowerShell handle UTF-8 sach hon batch).
REM -----------------------------------------------------------------------

cd /d "%~dp0"
title Briefa - Setup

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0SETUP.ps1"
