@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Nuclear Agent - Install

echo ==================================================
echo    NUCLEAR AGENT  -  installer  -  Nuclear Studios
echo ==================================================
echo.

rem ---------------------------------------------------------------- Python
set "PY="
where py >nul 2>nul
if errorlevel 1 goto :try_python
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto :try_python
set "PY=py -3"
goto :check_version

:try_python
rem (the Microsoft Store "python" shortcut fails this test, which is what we want)
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys" >nul 2>nul
if errorlevel 1 goto :no_python
set "PY=python"
goto :check_version

:no_python

echo [ERROR] Python was not found.
echo   1. Download Python 3.12 from https://www.python.org/downloads/windows/
echo   2. During setup, tick "Add python.exe to PATH".
echo   3. Run install.bat again.
pause
exit /b 1

:check_version
%PY% -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] else 1)"
if errorlevel 1 (
  echo [ERROR] Python 3.10 or newer is required. Found:
  %PY% --version
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PY% --version') do echo Found %%v

rem ------------------------------------------------------------- venv + deps
if exist ".venv\Scripts\python.exe" goto :have_venv
echo.
echo [1/4] Creating virtual environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 goto :fail_venv
:have_venv
set "VPY=.venv\Scripts\python.exe"

echo.
echo [2/4] Installing Python packages from PyPI ...
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail_pip

echo.
echo [3/4] Installing Playwright's Chromium browser ...
"%VPY%" -m playwright install chromium
if errorlevel 1 goto :fail_chromium

echo.
echo [4/4] Creating config.json ...
if exist config.json (
  echo config.json already exists - keeping your settings.
) else (
  copy /y config.example.json config.json >nul
  echo Created config.json from config.example.json
)

rem ------------------------------------------------------------------ Ollama
echo.
echo --------------------------------------------------
echo  Ollama - local AI
echo --------------------------------------------------
where ollama >nul 2>nul
if errorlevel 1 goto :no_ollama

echo Ollama is installed. Your models:
ollama list
echo.
echo Recommended for an 8 GB GPU: qwen2.5:7b - about 4.7 GB download.
choice /C YN /M "Download qwen2.5:7b now"
if errorlevel 2 goto :skip_pull
ollama pull qwen2.5:7b
:skip_pull
goto :tesseract

:no_ollama
echo Ollama was NOT found. Nuclear Agent needs it for the AI part.
echo   1. Download it from https://ollama.com/download
echo   2. Install it, then open a new terminal and run:  ollama pull qwen2.5:7b
echo The browser and control panel still work without it.

:tesseract
echo.
where tesseract >nul 2>nul
if not errorlevel 1 goto :tess_ok
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" goto :tess_ok
echo Optional: Tesseract OCR is not installed. It lets the agent read text inside
echo images or canvas pages. See README.md - "Optional: OCR".
goto :done
:tess_ok
echo Tesseract OCR found - OCR fallback is available.

:done
echo.
echo ==================================================
echo  Install finished. Run start.bat to launch Nuclear Agent.
echo ==================================================
pause
exit /b 0

:fail_venv
echo [ERROR] Could not create the virtual environment.
pause
exit /b 1
:fail_pip
echo [ERROR] Installing Python packages failed. Check your internet connection
echo and see the Troubleshooting section in README.md.
pause
exit /b 1
:fail_chromium
echo [ERROR] Installing Chromium failed. Try again, or run:
echo   .venv\Scripts\python.exe -m playwright install chromium
pause
exit /b 1
