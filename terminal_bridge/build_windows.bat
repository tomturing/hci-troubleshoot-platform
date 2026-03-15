@echo off
REM =============================================================================
REM terminal_bridge build script (run on Windows)
REM Output: terminal_bridge.exe (~3-4MB, ~1.5MB with upx)
REM Support: Win7 / Win10 / Win11, no runtime dependency
REM =============================================================================
REM Requirement: Go 1.21+
REM   Download: https://go.dev/dl/  choose go1.21.x.windows-amd64.msi
REM   After install, reopen this cmd window
REM =============================================================================

where go >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] go command not found.
    echo [INFO]  Please install Go first: https://go.dev/dl/
    echo [INFO]  After installation, reopen this window and run again.
    pause
    exit /b 1
)

echo [Build] Go version:
go version

echo [Build] Downloading dependencies...
go mod tidy
if %ERRORLEVEL% neq 0 (
    echo [ERROR] go mod tidy failed. Please check your network.
    pause
    exit /b 1
)

echo [Build] Compiling terminal_bridge.exe ...
set GOOS=windows
set GOARCH=amd64
set CGO_ENABLED=0
go build -ldflags="-s -w" -o terminal_bridge.exe .

if %ERRORLEVEL% neq 0 (
    echo [ERROR] Compilation failed.
    pause
    exit /b 1
)

echo.
for %%A in (terminal_bridge.exe) do echo [OK] Done: terminal_bridge.exe (%%~zA bytes)
echo.

REM Optional: compress with upx
where upx >nul 2>&1
if %ERRORLEVEL% equ 0 (
    echo [Build] upx found, compressing...
    upx --best terminal_bridge.exe
    for %%A in (terminal_bridge.exe) do echo [OK] Compressed: terminal_bridge.exe (%%~zA bytes)
) else (
    echo [INFO] Tip: install upx to shrink the binary to ~1.5MB
    echo [INFO]      Download: https://github.com/upx/upx/releases
)

echo.
echo [INFO] Next step:
echo   Copy terminal_bridge.exe to:
echo   [project]\frontend\customer\public\downloads\terminal_bridge.exe
echo.
echo   Then run the release script:
echo   bash scripts/k3s-release.sh --services customerUI
echo.
pause
