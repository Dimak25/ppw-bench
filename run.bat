@echo off
chcp 65001 >nul
py "%~dp0ppw_bench.py" %*
if errorlevel 1 python "%~dp0ppw_bench.py" %*
echo.
pause
