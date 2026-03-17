@echo off
title Building PG Alerts EXE
cd /d "%~dp0"
echo Building PG Alerts...
py -m PyInstaller --onefile --noconsole --add-data "sounds;sounds" --name "PG Alerts" pg_alerts.py
echo.
echo Done! EXE is in the dist\ folder.
pause
