@echo off
setlocal
cd /d "%~dp0"
title Nuclear Agent - internet tunnel

echo ==================================================================
echo  Nuclear Agent - internet tunnel - lets ChatGPT, your phone and the
echo  GitHub Pages panel reach the agent from outside this PC.
echo ==================================================================
echo.
echo Before you continue:
echo   1. Nuclear Agent must be running - start.bat.
echo   2. In the panel: Settings - AI connections - turn ON "Allow remote access".
echo   3. Create a key there. Without a valid key nobody gets in.
echo.

where cloudflared >nul 2>nul
if errorlevel 1 goto :no_cf

set "PORT=8765"
echo Starting tunnel to http://127.0.0.1:%PORT% ...
echo Look for a line with  https://SOMETHING.trycloudflare.com  below.
echo That is your public address. It changes every time you run this.
echo   MCP for AI apps:  https://SOMETHING.trycloudflare.com/mcp
echo   or, if the app cannot send headers:  .../mcp/k/YOUR_KEY
echo Close this window to shut the tunnel.
echo.
cloudflared tunnel --url http://127.0.0.1:%PORT%
pause
exit /b 0

:no_cf
echo cloudflared is not installed. It is Cloudflare's official free tunnel program.
echo Install it with this command in a terminal, then run remote.bat again:
echo.
echo     winget install --id Cloudflare.cloudflared
echo.
echo Official page: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
pause
exit /b 1
