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

REM --- Check the project files this app imports -------------------------
REM These ship with the app rather than being installed by setup.bat, so they
REM go missing when someone updates by hand-copying a few files instead of the
REM whole folder. Without this check that surfaces as a raw ImportError.
REM
REM audit_gui imports camera_engine, fleet_catalog and ui_theme, none of which
REM import another project file -- so this list is deliberately SHORTER than
REM the writer's. Sharing one list would block this tool over aoa_config /
REM vendor_adapter / hik_config, which the audit path never touches.
set "CORE_FILES=audit_gui.py ui_theme.py camera_engine.py fleet_catalog.py"

REM Two passes (flag, then list) so every missing name can be printed without
REM delayed expansion -- which would eat a "!" in the folder path, and these
REM launchers run from wherever the operator unpacked the folder.
set "MISSING="
for %%F in (%CORE_FILES%) do if not exist "%~dp0%%F" set "MISSING=1"

if defined MISSING (
  echo ============================================================
  echo   Files this app needs are missing:
  echo.
  for %%F in (%CORE_FILES%) do if not exist "%~dp0%%F" echo       %%F
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
  REM The parens MUST stay escaped: an unescaped ")" here closes the
  REM "if errorlevel 1 (" block early and this message dies with
  REM ". was unexpected at this time." instead of printing.
  echo   The app closed with an error ^(shown above^).
  echo   Copy this text and send it to Marc if you need help.
  echo ============================================================
  echo.
  pause
)
