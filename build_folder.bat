@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   电柜快画 - 打包文件夹版（闪退排查用）
echo ============================================
echo.

where py >nul 2>nul
if errorlevel 1 (
  set PY=python
) else (
  set PY=py
)

%PY% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
  echo 正在安装 PyInstaller ...
  %PY% -m pip install pyinstaller
  if errorlevel 1 goto :fail
)

echo 清理旧构建 ...
if exist build_onedir rmdir /s /q build_onedir
if exist "dist\电柜快画_文件夹版" rmdir /s /q "dist\电柜快画_文件夹版"

echo 开始打包（约 1-2 分钟）...
%PY% -m PyInstaller ^
  --noconfirm ^
  --onedir ^
  --windowed ^
  --name "电柜快画_文件夹版" ^
  --distpath "dist" ^
  --workpath "build_onedir" ^
  --specpath "build_onedir" ^
  --add-data "%~dp0data;data" ^
  --hidden-import matplotlib.backends.backend_tkagg ^
  --hidden-import matplotlib.backends.backend_pdf ^
  --hidden-import matplotlib.backends.backend_svg ^
  --hidden-import matplotlib.backends.backend_agg ^
  --collect-data matplotlib ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module IPython ^
  --exclude-module jupyter ^
  --exclude-module pytest ^
  main.py
if errorlevel 1 goto :fail

echo.
echo 打包完成：dist\电柜快画_文件夹版\
echo 复制时请整个文件夹一起复制，不能只拷 exe。
pause
exit /b 0

:fail
echo.
echo 打包失败，请查看上方错误信息。
pause
exit /b 1
