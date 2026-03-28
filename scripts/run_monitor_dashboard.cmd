@echo off
set "PYTHONPATH=C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent;%PYTHONPATH%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_monitor_dashboard.ps1" -ProjectRoot "C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent" -OpenBrowser
