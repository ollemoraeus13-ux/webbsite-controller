@echo off
setlocal
cd /d "%~dp0"
title Nuclear Agent - connect to Claude Desktop
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
echo This adds Nuclear Agent as a tool in Claude Desktop on this PC.
echo A backup of your current Claude config is saved first.
echo.
".venv\Scripts\python.exe" -m nuclear_agent.setup_claude %*
echo.
pause
