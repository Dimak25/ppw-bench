@echo off
chcp 65001 >nul
rem Pick the interpreter up front: checking errorlevel after the run
rem used to start the benchmark a second time when the script failed.
where py >nul 2>nul
if not errorlevel 1 (
    py "%~dp0ppw_bench.py" %*
) else (
    python "%~dp0ppw_bench.py" %*
)
echo.
pause
