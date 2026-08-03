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
