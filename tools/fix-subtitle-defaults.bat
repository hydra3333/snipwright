@echo off
rem Start the subtitle-defaults repair tool with Snipwright's own Python,
rem so it has PySide6 without anything else being installed.
set "HERE=%~dp0"
set "PY=%HERE%..\.venv\Scripts\pythonw.exe"
if exist "%PY%" (
    start "" "%PY%" "%HERE%fix-subtitle-defaults.py"
) else (
    start "" pythonw "%HERE%fix-subtitle-defaults.py"
)
