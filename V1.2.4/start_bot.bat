@echo off
rem Start the wc-chatbot main loop.
rem Usage: start_bot.bat [logfile]     default: run_bot.log
setlocal
cd /d "%~dp0"
set "LOG=%~1"
if "%LOG%"=="" set "LOG=run_bot.log"
set "ERR=%~dpn1.err"
if "%ERR%"=="" set "ERR=run_bot.err"
set "PY="
set "PYARG="

call :probe "%~dp0python\python.exe"
if not defined PY call :probe "%WX_PYTHON%"
if not defined PY call :probe python
if not defined PY call :probe python3
if not defined PY call :probe py -3

if not defined PY (
  echo.
  echo [start_bot] ?????? Python?
  echo   1^) ?????????????? python\python.exe ^???????^?
  echo   2^) ??????? WX_PYTHON ???? python.exe
  echo   3^) ??? Python 3.12 ?? PATH
  echo.
  pause
  exit /b 2
)

echo ==== start %date% %time% ====  1>>"%LOG%"
echo [start_bot] python = %PY%  1>>"%LOG%"
"%PY%" %PYARG% wx_multi.py --rooms rooms.json --rounds 999999 --gap 2  1>>"%LOG%" 2>>"%ERR%"
set "RC=%errorlevel%"
echo ==== exit %date% %time% code=%RC% ====  1>>"%LOG%"
if not "%RC%"=="0" (
  echo.
  echo [start_bot] ????? ^(code=%RC%^)????????
  if exist "%LOG%" powershell -NoProfile -Command "Get-Content -Tail 6 -LiteralPath '%LOG%'"
  if exist "%ERR%" powershell -NoProfile -Command "Get-Content -Tail 6 -LiteralPath '%ERR%'"
  echo.
  echo [start_bot] ?? 20 ???????????
  timeout /t 20 >nul 2>nul
)
exit /b %RC%

:probe
if "%~1"=="" exit /b 0
"%~1" %2 -V >nul 2>nul || exit /b 0
set "PY=%~1"
set "PYARG=%2"
exit /b 0
