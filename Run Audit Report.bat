@echo off
setlocal
cd /d "%~dp0"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo ============================================================
  echo   This app isn't set up on this computer yet.
  echo.
  echo   Double-click "setup.bat" first, let it finish, then
  echo   run this again.
  echo ============================================================
  echo.
  pause
  exit /b 1
)

REM Shipped alongside the app, not installed by setup.bat -- so it goes missing
REM when someone updates by hand-copying a few files instead of copying the
REM whole folder. Without this check that shows up as a raw ImportError.
if not exist "%~dp0ui_theme.py" (
  echo ============================================================
  echo   A file this app needs is missing: ui_theme.py
  echo.
  echo   This usually means the folder was updated by copying only
  echo   some of the files. Copy the WHOLE "axis_api_testing"
  echo   folder over again, then run this once more.
  echo ============================================================
  echo.
  pause
  exit /b 1
)

"%VENV_PY%" "%~dp0audit_gui.py"
if errorlevel 1 (
  echo.
  echo ============================================================
  echo   The app closed with an error (shown above).
  echo   Copy this text and send it to Marc if you need help.
  echo ============================================================
  echo.
  pause
)
