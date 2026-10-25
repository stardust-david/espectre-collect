@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"

echo.
echo ====================================================================
echo   ESPectre one-click pipeline  (self-check -^> collect -^> train)
echo ====================================================================
echo.

where powershell >nul 2>&1
if errorlevel 1 (
    echo [x] PowerShell not found. Windows with PowerShell 5.1 or later is required.
    echo.
    pause
    exit /b 2
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [DONE] Pipeline finished successfully.
) else (
    echo [EXIT] Pipeline finished with code %RC%.
)
echo.
pause
exit /b %RC%
