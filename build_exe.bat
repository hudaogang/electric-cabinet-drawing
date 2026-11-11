@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   电柜快画 - 打包单文件 EXE
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
  echo [1/3] 安装 PyInstaller ...
  %PY% -m pip install pyinstaller
  if errorlevel 1 goto :fail
)

echo [2/3] 清理旧构建 ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "电柜快画.spec" del /q "电柜快画.spec"

echo [3/3] 开始打包（约 1-3 分钟）...
%PY% -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name "电柜快画" ^
  --add-data "data;data" ^
  --hidden-import matplotlib.backends.backend_tkagg ^
  --hidden-import matplotlib.backends.backend_pdf ^
  --hidden-import matplotlib.backends.backend_svg ^
  --hidden-import matplotlib.backends.backend_agg ^
  --hidden-import dkk.dxf ^
  --collect-data matplotlib ^
  --exclude-module PyQt5 ^
  --exclude-module PyQt6 ^
  --exclude-module PySide2 ^
  --exclude-module PySide6 ^
  --exclude-module IPython ^
  --exclude-module jupyter ^
  --exclude-module pytest ^
  --exclude-module matplotlib.tests ^
  --exclude-module matplotlib.testing ^
  --exclude-module numpy.tests ^
  --exclude-module tkinter.test ^
  --exclude-module matplotlib.backends.backend_qt5agg ^
  --exclude-module matplotlib.backends.backend_qtagg ^
  --exclude-module matplotlib.backends.backend_webagg ^
  --exclude-module matplotlib.backends.backend_webagg_core ^
  --exclude-module matplotlib.backends.backend_nbagg ^
  --exclude-module matplotlib.backends.backend_wx ^
  --exclude-module matplotlib.backends.backend_wxagg ^
  --exclude-module matplotlib.backends.backend_gtk3 ^
  --exclude-module matplotlib.backends.backend_gtk3agg ^
  --exclude-module matplotlib.backends.backend_gtk4 ^
  --exclude-module matplotlib.backends.backend_gtk4agg ^
  --exclude-module matplotlib.backends.backend_macosx ^
  --exclude-module matplotlib.backends.backend_template ^
  main.py
if errorlevel 1 goto :fail

echo.
echo 打包完成：dist\电柜快画.exe
echo 首次启动较慢（单文件解压），属正常现象。
pause
exit /b 0

:fail
echo.
echo 打包失败，请查看上方错误信息。
pause
exit /b 1
