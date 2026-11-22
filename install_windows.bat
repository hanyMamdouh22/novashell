@echo off
setlocal enabledelayedexpansion

echo ==========================================
echo   Installing NovaShell for Windows...
echo ==========================================

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10+ is not installed or not in PATH.
    echo Download it from https://www.python.org/downloads/ and check "Add Python to PATH".
    pause
    exit /b 1
)

set "SRC_DIR=%~dp0"
set "INSTALL_DIR=%LOCALAPPDATA%\NovaShell"
set "BIN_DIR=%INSTALL_DIR%\bin"

if exist "%INSTALL_DIR%\novashell" rmdir /s /q "%INSTALL_DIR%\novashell"
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"

xcopy "%SRC_DIR%novashell" "%INSTALL_DIR%\novashell\" /E /I /Y /Q >nul

if exist "%SRC_DIR%default_plugins" (
    xcopy "%SRC_DIR%default_plugins" "%INSTALL_DIR%\default_plugins\" /E /I /Y /Q >nul
)

echo Creating isolated Python environment...
if not exist "%INSTALL_DIR%\.venv" (
    python -m venv "%INSTALL_DIR%\.venv"
)

echo Installing dependencies (google-genai, rich)...
"%INSTALL_DIR%\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
"%INSTALL_DIR%\.venv\Scripts\pip.exe" install google-genai rich -q

echo Creating nova.cmd launcher...
(
echo @echo off
echo setlocal
echo set "PYTHONPATH=%%LOCALAPPDATA%%\NovaShell;%%PYTHONPATH%%"
echo "%%LOCALAPPDATA%%\NovaShell\.venv\Scripts\python.exe" -m novashell.cli %%*
) > "%BIN_DIR%\nova.cmd"

echo Adding NovaShell to User PATH...
powershell -NoProfile -Command "$p = [Environment]::GetEnvironmentVariable('Path', 'User'); if ($p -notlike '*%BIN_DIR%*') { [Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';%BIN_DIR%'), 'User') }"

echo.
echo ==========================================
echo   Done! Close and reopen PowerShell/CMD,
echo   then run: nova setup
echo ==========================================
pause
