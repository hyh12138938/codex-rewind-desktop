@echo off
cd /d "%~dp0"
call "%~dp0NATIVE_STOP.cmd"
python install.py --uninstall
pause
