@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Nuclear Agent

if not exist ".venv\Scripts\python.exe" (
  echo Nuclear Agent is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
if not exist config.json copy /y config.example.json config.json >nul

rem Start Ollama in the background if it is installed but not answering.
where ollama >nul 2>nul
if errorlevel 1 (
  echo [warning] Ollama not found - the AI will not work until you install it from https://ollama.com/download
  goto :run
)
curl -s -m 2 http://127.0.0.1:11434/api/version >nul 2>nul
if errorlevel 1 (
  echo Starting Ollama ...
  start "Ollama" /min ollama serve
  timeout /t 3 /nobreak >nul
)

:run
".venv\Scripts\python.exe" -m nuclear_agent %*
if errorlevel 1 pause
