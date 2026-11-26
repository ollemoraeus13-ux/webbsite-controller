@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
echo Running Nuclear Agent tests - a hidden Chromium will open and close many times.
".venv\Scripts\python.exe" -m pytest -v tests
pause
