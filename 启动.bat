@echo off
rem 日迹 · 本地日记：双击打开
cd /d "%~dp0"
if not exist "data" mkdir data

rem 首次运行：在桌面建一个图标，以后双击图标即可
set "LNK=%USERPROFILE%\Desktop\日迹.lnk"
if not exist "%LNK%" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');$s.TargetPath='%~f0';$s.WorkingDirectory='%~dp0';$s.IconLocation='%~dp0app\riji.ico,0';$s.Save()" >nul 2>nul
)

where pythonw >nul 2>nul
if errorlevel 1 (
  echo 没找到 Python 3.9 以上，请先安装：https://www.python.org/downloads/
  echo 安装时记得勾选 Add Python to PATH。
  pause
  exit /b 1
)

start "" pythonw "%~dp0app\server.py" --data "%~dp0data" --port 8888 --who owner --title 日迹 --lan --app
