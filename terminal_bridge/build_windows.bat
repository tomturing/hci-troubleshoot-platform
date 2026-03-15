@echo off
REM =============================================================================
REM terminal_bridge 打包脚本（Windows上执行）
REM 输出: dist\terminal_bridge.exe （单文件，无需安装）
REM =============================================================================
REM
REM 使用方法:
REM   1. 在此目录执行: build_windows.bat
REM   2. 打包完成后，将 dist\terminal_bridge.exe
REM      复制到: frontend\customer\public\downloads\terminal_bridge.exe
REM
REM 前置条件:
REM   Python 3.10+，已安装 pip
REM
REM =============================================================================

echo [Build] 安装依赖...
pip install -r requirements.txt pyinstaller --quiet
if %ERRORLEVEL% neq 0 (
    echo [ERROR] 依赖安装失败
    pause
    exit /b 1
)

echo [Build] 开始打包...
pyinstaller ^
    --onefile ^                             
    --noconsole ^                           
    --name terminal_bridge ^               
    --hidden-import websockets ^           
    --hidden-import pystray ^              
    --hidden-import PIL ^                  
    main.py

if %ERRORLEVEL% neq 0 (
    echo [ERROR] PyInstaller 打包失败
    pause
    exit /b 1
)

echo.
echo [OK] 打包完成: dist\terminal_bridge.exe
echo.
echo [INFO] 下一步:
echo   将 dist\terminal_bridge.exe 复制到 项目目录\frontend\customer\public\downloads\
echo   然后运行发布脚本： bash scripts/k3s-release.sh --services customerUI
echo.
pause
