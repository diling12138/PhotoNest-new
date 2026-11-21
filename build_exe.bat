@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "models\manifest.json" (
  echo 请先按 docs\ai-setup.md 准备离线模型。
  goto :err
)
if not exist "_build\ai-env\Scripts\python.exe" (
  python -m venv --system-site-packages _build\ai-env
  if errorlevel 1 goto :err
)
"_build\ai-env\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :err
"_build\ai-env\Scripts\python.exe" tools\build_release.py
if errorlevel 1 goto :err
echo 完成：PhotoNest.exe 和 PhotoNest-win64.zip
echo 分发时保持 PhotoNest.exe、_internal、models 在同一目录。
pause
exit /b 0
:err
echo 打包失败，请检查上方信息。
pause
exit /b 1
