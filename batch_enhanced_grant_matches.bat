@echo off
REM Launcher for the enhanced grant matching rebuild workflow.
REM The real workflow lives in batch_enhanced_grant_matches.ps1 so commands can
REM use PowerShell's safer argument handling and fail-fast behavior.

setlocal
set "SCRIPT_DIR=%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%batch_enhanced_grant_matches.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
  echo Enhanced grant matching workflow failed with exit code %EXIT_CODE%.
) else (
  echo Enhanced grant matching workflow completed successfully.
)
echo.
pause
exit /b %EXIT_CODE%
