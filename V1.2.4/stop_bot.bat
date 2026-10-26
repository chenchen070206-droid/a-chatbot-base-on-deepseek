@echo off
chcp 65001 >nul
rem ============================================================
rem  One-click stop for wc-chatbot scripts
rem    stop_bot.bat          stop scripts only
rem    stop_bot.bat wechat   also close WeChat
rem  Real work is done by stop_bot.ps1 in the same folder.
rem ============================================================
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_bot.ps1" %*
if errorlevel 1 echo stop_bot.ps1 failed, see messages above.
pause
