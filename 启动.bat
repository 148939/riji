@echo off
rem 日迹 · 本地版：双击即可
cd /d "%~dp0"
if not exist "data" mkdir data

set PY=
if exist "%~dp0python\pythonw.exe" set PY="%~dp0python\pythonw.exe"
if not defined PY (
  where pythonw >nul 2>nul && set PY=pythonw
)
if not defined PY (
  where python >nul 2>nul && set PY=python
)
if not defined PY (
  echo 需要 Python 3.9 以上：https://www.python.org/downloads/
  echo 安装时勾选 Add Python to PATH。
  pause
  exit /b 1
)
start "" %PY% "%~dp0app\server.py" --data "%~dp0data" --port 8765 --app
