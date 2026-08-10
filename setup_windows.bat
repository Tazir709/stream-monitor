@echo off
setlocal

echo Stream Monitor (Windows) - Setup
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH. Install it from https://www.python.org/downloads/
    echo         and make sure "Add python.exe to PATH" is checked during install.
    goto :error
)

if not exist Stream_Venv (
    echo Creating virtual environment...
    python -m venv Stream_Venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        goto :error
    )
)

echo Installing dependencies...
call Stream_Venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate the virtual environment.
    goto :error
)

python -m pip install --upgrade pip
pip install PySide6 psutil yt-dlp "curl_cffi<0.16"
if errorlevel 1 (
    echo [ERROR] Dependency install failed. See the output above.
    goto :error
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo.
    echo [WARNING] ffmpeg was not found on PATH. Stream Monitor uses it for
    echo           live thumbnail previews and to record streams. Grab a build
    echo           from https://www.gyan.dev/ffmpeg/builds/ and add its bin\
    echo           folder to PATH.
)

echo.
echo Setup complete!
echo.
echo Just double-click stream_manager.py to run it - it launches itself
echo under Stream_Venv automatically, no terminal or typing needed, and
echo opens with no console window.
echo.
pause
endlocal
exit /b 0

:error
echo.
echo Setup did not finish. See the error above.
echo.
pause
endlocal
exit /b 1
