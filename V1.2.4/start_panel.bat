@echo off
rem Start the local control panel (http://127.0.0.1:8790).
rem Usage: start_panel.bat [port]     default: 8790
rem Python lookup order: .\python\python.exe (installer layout) -> %WX_PYTHON% -> python / python3 / py -3
setlocal EnableExtensions
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8790"
set "PY="
set "PYARG="

call :probe "%~dp0python\python.exe"
if defined PY goto run
call :probe "%WX_PYTHON%"
if defined PY goto run
call :probe "python"
if defined PY goto run
call :probe "python3"
if defined PY goto run
call :probe py -3
if defined PY goto run

echo ============================================================
echo  [start_panel] No usable Python found.
echo    1) installed package layout: expect "%~dp0python\python.exe"
echo    2) or set environment variable WX_PYTHON to your python.exe
echo    3) or add Python to PATH
echo ============================================================
timeout /t 20 >nul 2>nul
exit /b 2

:probe
if "%~1"=="" exit /b 0
"%~1" %2 -V >nul 2>nul
if errorlevel 1 exit /b 0
set "PY=%~1"
set "PYARG=%2"
exit /b 0

:run
start "wcb-panel" "%PY%" %PYARG% panel.py --port %PORT%
