@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "_internal\_tcl_data\init.tcl" set TCL_LIBRARY=%CD%\_internal\_tcl_data
if exist "_internal\_tk_data\tk.tcl" set TK_LIBRARY=%CD%\_internal\_tk_data
if exist "_build\ai-env\Scripts\python.exe" (
  "_build\ai-env\Scripts\python.exe" app.py
) else (
  python app.py
)
if errorlevel 1 pause
