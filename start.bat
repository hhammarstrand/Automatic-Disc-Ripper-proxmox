@echo off
setlocal
REM ============================================================
REM  Automatic Disc Ripper for Windows - Start script
REM ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Run install.bat first.
    pause
    exit /b 1
)

if not exist "config\adr.yaml" (
    echo [NOTE] config\adr.yaml not found - using default values.
    echo       Run the setup GUI to configure:
    echo         .venv\Scripts\python.exe setup_gui.py
    echo.
)

echo Starting Automatic Disc Ripper for Windows...
echo Press Ctrl+C to stop.
echo.
.venv\Scripts\python.exe run.py %*
