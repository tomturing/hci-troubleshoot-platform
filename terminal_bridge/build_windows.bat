@echo off
chcp 65001 >nul
REM =============================================================================
REM terminal_bridge Go 版编译脚本（在 Windows 上执行）
REM 输出: terminal_bridge.exe  (~3-4MB，upx 压缩后 ~1.5MB)
REM 支持: Win7 / Win10 / Win11，无运行时依赖
REM =============================================================================
REM 前置条件: 安装 Go 1.21+
REM   下载地址: https://go.dev/dl/  选择 go1.21.x.windows-amd64.msi
REM   安装后重新打开此 cmd 窗口
REM =============================================================================

where go >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] 未找到 go 命令
    echo [INFO]  请先安装 Go: https://go.dev/dl/
    echo [INFO]  安装完成后重新打开此窗口再运行
    pause
    exit /b 1
)

echo [Build] Go 版本:
go version

echo [Build] 下载依赖...
go mod tidy
if %ERRORLEVEL% neq 0 (
    echo [ERROR] 依赖下载失败，请检查网络
    pause
    exit /b 1
)

echo [Build] 编译 terminal_bridge.exe ...
set GOOS=windows
set GOARCH=amd64
set CGO_ENABLED=0
go build -ldflags="-s -w" -o terminal_bridge.exe .

if %ERRORLEVEL% neq 0 (
    echo [ERROR] 编译失败
    pause
    exit /b 1
)

echo.
for %%A in (terminal_bridge.exe) do echo [OK] 编译完成: terminal_bridge.exe (%%~zA bytes)
echo.

REM 可选：用 upx 压缩（需要先安装 upx）
where upx >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [Build] 检测到 upx，正在压缩...
    upx --best terminal_bridge.exe
    for %%A in (terminal_bridge.exe) do echo [OK] 压缩后: terminal_bridge.exe (%%~zA bytes)
) else (
    echo [INFO] 提示: 安装 upx 可进一步压缩体积到约 1.5MB
    echo [INFO]       下载: https://github.com/upx/upx/releases
)

echo.
echo [INFO] 下一步:
echo   将 terminal_bridge.exe 复制到:
echo   项目目录\frontend\customer\public\downloads\terminal_bridge.exe
echo.
echo   然后运行发布脚本:
echo   bash scripts/k3s-release.sh --services customerUI
echo.
pause
