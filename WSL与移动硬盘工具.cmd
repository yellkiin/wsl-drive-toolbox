@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0start.py" %*
    exit /b 0
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0start.py" %*
    exit /b 0
)
echo 请安装包含 Tcl/Tk 的 Windows Python 3.10 或更新版本。
echo 或下载便携版，启动 WSL-Drive-Toolbox.exe。
pause
exit /b 1
