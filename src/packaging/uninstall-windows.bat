@echo off
REM ---------------------------------------------------------------------------
REM  Snipwright - Windows uninstaller (Command Prompt friendly)
REM
REM  Double-click this file, or run it from a Command Prompt:
REM      uninstall-windows.bat
REM
REM  It just launches uninstall-windows.ps1 with the right flags so you don't
REM  have to deal with PowerShell's execution policy yourself - the -Bypass
REM  applies only to this single run and changes nothing permanently.  The
REM  PowerShell script asks before removing anything and pauses at the end
REM  itself, so this wrapper doesn't.
REM ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%uninstall-windows.ps1"
