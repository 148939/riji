@echo off
rem Riji local diary - double click to open
cd /d "%~dp0"
if not exist "data" mkdir data
if not exist "%USERPROFILE%\Desktop\»’º£.lnk" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\make-icon.ps1" "%~f0" "%~dp0" >nul 2>nul

where pythonw >nul 2>nul
if errorlevel 1 goto nopy

start "" pythonw "%~dp0app\server.py" --data "%~dp0data" --port 8888 --who owner --title »’º£ --lan --app
exit /b 0

:nopy
echo Need Python 3.9+ : https://www.python.org/downloads/
echo Install it and tick "Add Python to PATH".
pause
exit /b 1
