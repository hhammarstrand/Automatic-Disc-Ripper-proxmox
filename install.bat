@echo off
setlocal enabledelayedexpansion
REM ============================================================
REM  Automatic Disc Ripper for Windows - Installation script
REM  Run this from the project folder on the ripping machine.
REM
REM  Python download: To change the version, update
REM  PY_VERSION and PY_URL below.
REM ============================================================

set "PY_VERSION=3.12.9"
set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"

echo.
echo  ============================================
echo   Automatic Disc Ripper for Windows Installation
echo  ============================================
echo.

REM --- Check that we are in the correct folder ---
if not exist "run.py" (
    echo [ERROR] run.py not found. Run this script from the project folder.
    echo         Example: cd C:\GitHub\automatic-disc-ripper
    echo                  install.bat
    pause
    exit /b 1
)

REM ============================================================
REM  Step 1 — Python
REM ============================================================
echo [1/6] Checking Python...

set "PYTHON_CMD="

REM Try the "py" launcher first (most reliable on Windows)
py --version >nul 2>&1
if !ERRORLEVEL! equ 0 (
    for /f "tokens=*" %%v in ('py --version 2^>^&1') do (
        echo %%v | findstr /i "Python 3" >nul 2>&1
        if !ERRORLEVEL! equ 0 (
            set "PYTHON_CMD=py"
            goto :python_found
        )
    )
)

REM Try "python" and verify it is not the Store alias
for /f "tokens=*" %%v in ('python --version 2^>^&1') do (
    echo %%v | findstr /i "Python 3" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_CMD=python"
        goto :python_found
    )
)

REM Try "python3"
for /f "tokens=*" %%v in ('python3 --version 2^>^&1') do (
    echo %%v | findstr /i "Python 3" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_CMD=python3"
        goto :python_found
    )
)

REM --- Python not found - offer automatic installation ---
echo [!!] Python was not found on this computer.
echo.
set /p INSTALL_PY="     Would you like to download and install Python automatically? (Y/N): "
if /i "!INSTALL_PY!" neq "Y" (
    echo.
    echo     Install Python manually from https://www.python.org/downloads/
    echo     IMPORTANT: Check "Add python.exe to PATH"
    echo.
    echo     Tip: Disable Windows Store aliases for python:
    echo     Settings ^> Apps ^> Advanced app settings ^> App execution aliases
    pause
    exit /b 1
)

echo.
echo     Downloading Python %PY_VERSION%...
set "PY_INSTALLER=%TEMP%\python-installer.exe"
powershell -NoProfile -Command "& { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_INSTALLER%' }"
if !ERRORLEVEL! neq 0 (
    echo [ERROR] Could not download Python. Check your internet connection.
    echo         URL: %PY_URL%
    pause
    exit /b 1
)
echo     Download complete.
echo.
echo     Installing Python %PY_VERSION% (this may take a minute)...
echo     NOTE: If a UAC prompt appears, click "Yes".
"%PY_INSTALLER%" /passive InstallAllUsers=1 PrependPath=1 Include_test=0
if !ERRORLEVEL! neq 0 (
    echo [ERROR] Python installation failed.
    echo         Try running the installer manually: %PY_INSTALLER%
    pause
    exit /b 1
)
echo     [OK] Python installed!
echo.

REM Update PATH in this session so py/python can be found
for /f "tokens=1,2 delims=." %%a in ("%PY_VERSION%") do set "PY_MAJOR=%%a%%b"
set "PATH=C:\Program Files\Python%PY_MAJOR%;C:\Program Files\Python%PY_MAJOR%\Scripts;%PATH%"

REM Verify that it works now
py --version >nul 2>&1
if !ERRORLEVEL! equ 0 (
    set "PYTHON_CMD=py"
) else (
    python --version >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_CMD=python"
    ) else (
        echo [ERROR] Python was installed but could not be found in PATH.
        echo         Restart the command prompt and run install.bat again.
        pause
        exit /b 1
    )
)

:python_found
for /f "tokens=*" %%v in ('!PYTHON_CMD! --version 2^>^&1') do set PYVER=%%v
echo       [OK] !PYVER! found

REM ============================================================
REM  Step 2 — Virtual environment
REM ============================================================
echo.
echo [2/6] Creating Python virtual environment (.venv)...
if exist ".venv\Scripts\python.exe" (
    echo       .venv already exists, skipping.
) else (
    if exist ".venv" rmdir /s /q ".venv"
    !PYTHON_CMD! -m venv .venv
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] Could not create virtual environment.
        pause
        exit /b 1
    )
    echo       Created.
)

REM ============================================================
REM  Step 3 — Python dependencies
REM ============================================================
echo.
echo [3/6] Installing Python dependencies...
.venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
.venv\Scripts\python.exe -m pip install -r requirements.txt >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [ERROR] pip install failed. Run manually for details:
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
echo       [OK] All dependencies installed.

REM ============================================================
REM  Step 4 — Check MakeMKV and HandBrakeCLI
REM ============================================================
echo.
echo [4/6] Checking MakeMKV and HandBrakeCLI...

set "MAKEMKV_FOUND=0"
if exist "C:\Program Files (x86)\MakeMKV\makemkvcon64.exe" set "MAKEMKV_FOUND=1"
if exist "C:\Program Files\MakeMKV\makemkvcon64.exe" set "MAKEMKV_FOUND=1"
where makemkvcon64 >nul 2>&1 && set "MAKEMKV_FOUND=1"

if "!MAKEMKV_FOUND!"=="1" (
    echo       [OK] MakeMKV found
) else (
    echo       [!!] MakeMKV was NOT found.
    echo           Download from: https://www.makemkv.com/download/
    echo           Then update makemkv_path in config\adr.yaml
)

set "HB_FOUND=0"
if exist "C:\Program Files\HandBrake\HandBrakeCLI.exe" set "HB_FOUND=1"
if exist "C:\Program Files (x86)\HandBrake\HandBrakeCLI.exe" set "HB_FOUND=1"
where HandBrakeCLI >nul 2>&1 && set "HB_FOUND=1"

if "!HB_FOUND!"=="1" (
    echo       [OK] HandBrakeCLI found
) else (
    echo       [!!] HandBrakeCLI was NOT found.
    echo           Download the CLI version from: https://handbrake.fr/downloads2.php
    echo           Extract HandBrakeCLI.exe to C:\Program Files\HandBrake\
    echo           Then update handbrake_path in config\adr.yaml
)

REM ============================================================
REM  Step 5 — Configuration
REM ============================================================
echo.
echo [5/6] Checking configuration...
if not exist "config\adr.yaml" (
    if exist "config\adr.yaml.example" (
        copy "config\adr.yaml.example" "config\adr.yaml" >nul
        echo       [OK] config\adr.yaml created from example configuration.
        echo           Edit the file to customize paths, TMDb key, etc.
    ) else (
        echo       [!!] Neither config\adr.yaml nor adr.yaml.example was found.
        echo           The app will start with defaults but you should create a config.
    )
) else (
    echo       [OK] config\adr.yaml already exists.
)

REM ============================================================
REM  Step 6 — Output directories
REM ============================================================
echo.
echo [6/6] Creating output directories...
if not exist "C:\ADR\raw" mkdir "C:\ADR\raw"
if not exist "C:\ADR\completed" mkdir "C:\ADR\completed"
echo       C:\ADR\raw         (temporary MKV files)
echo       C:\ADR\completed   (finished MP4 files)

REM ============================================================
REM  Done!
REM ============================================================
echo.
echo ============================================
echo  Installation complete!
echo ============================================
echo.
echo  Next steps:
echo    1. Configure Automatic Disc Ripper for Windows (paths, TMDb key, etc.)
echo       You can do this via the setup GUI, the web UI, or by
echo       editing config\adr.yaml manually.
echo.
echo    2. Start Automatic Disc Ripper for Windows:
echo       start.bat
echo.
echo    3. Open the web interface in your browser:
echo       http://localhost:8080
echo.
echo  If you want to access the web UI from other computers on
echo  your LAN, open port 8080 in the firewall (run as admin):
echo    netsh advfirewall firewall add rule name="Automatic Disc Ripper for Windows" ^
echo      dir=in action=allow protocol=TCP localport=8080
echo.

set /p LAUNCH_GUI="Would you like to open the setup GUI now to configure? (Y/N): "
if /i "!LAUNCH_GUI!"=="Y" (
    echo.
    echo Starting setup GUI...
    .venv\Scripts\python.exe setup_gui.py
) else (
    echo.
    echo You can run the setup GUI later with:
    echo   .venv\Scripts\python.exe setup_gui.py
)
echo.
pause
