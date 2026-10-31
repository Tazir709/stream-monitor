@echo off
setlocal

set "APP_DIR=%~dp0"
set "PYTHONW=%APP_DIR%Stream_Venv\Scripts\pythonw.exe"
set "APP=%APP_DIR%stream_manager.py"

if not exist "%PYTHONW%" (
    echo [ERROR] Stream_Venv\Scripts\pythonw.exe was not found.
    echo Run setup_windows.bat first.
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo [ERROR] stream_manager.py was not found in "%APP_DIR%".
    pause
    exit /b 1
)

start "" "%PYTHONW%" "%APP%" %*
endlocal
