@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_supervisor_watchdog.ps1" -ProjectRoot "C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent"
