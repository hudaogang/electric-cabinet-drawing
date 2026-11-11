@echo off
chcp 65001 >nul
cd /d "%~dp0.."
py -m unittest discover -s tests -v
pause
