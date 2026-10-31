@echo off
setlocal

set "APP_DIR=%~dp0"
set "PYTHON=%APP_DIR%Stream_Venv\Scripts\python.exe"
set "APP=%APP_DIR%stream_manager.py"

if not exist "%PYTHON%" (
    echo [ERROR] Stream_Venv\Scripts\python.exe was not found.
    echo Run setup_windows.bat first.
    pause
    exit /b 1
)

if not exist "%APP%" (
    echo [ERROR] stream_manager.py was not found in "%APP_DIR%".
    pause
    exit /b 1
)

"%PYTHON%" "%APP%" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Stream Monitor exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%
