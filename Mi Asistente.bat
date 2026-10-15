@echo off
rem Opens Mi Asistente without a console window (double-click this file, or make a shortcut to it).
rem Needs the .venv folder created by the setup steps in README.md.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" main.py
) else (
    echo No encuentro la carpeta .venv. Sigue los pasos de instalacion del README.md.
    pause
)
