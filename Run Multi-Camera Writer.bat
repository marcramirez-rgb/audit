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
REM Same reasoning as the other launchers: these ship with the app rather than
REM being installed by setup.bat, so they go missing when someone updates by
REM hand-copying a few files. Without this check that surfaces as a raw
REM ImportError instead of a readable message.
REM
REM Import chain for this tool -- it SUBCLASSES the single-camera writer, so it
REM needs everything that one needs, plus itself:
REM   multi_writer_gui     -> analytics_writer_gui, vendor_adapter
REM   analytics_writer_gui -> aoa_config, vendor_adapter, fleet_catalog, ui_theme
REM   vendor_adapter       -> aoa_config, camera_engine, hik_config, pd_config
REM   aoa_config           -> camera_engine
REM   hik_config           -> camera_engine
REM   pd_config            -> camera_engine
set "CORE_FILES=multi_writer_gui.py analytics_writer_gui.py ui_theme.py aoa_config.py vendor_adapter.py hik_config.py pd_config.py camera_engine.py fleet_catalog.py"

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

"%VENV_PY%" "%~dp0multi_writer_gui.py"
if errorlevel 1 (
  echo.
  echo ============================================================
  REM The parens MUST stay escaped: an unescaped ")" here closes the
  REM "if errorlevel 1 (" block early and this message dies with
  REM ". was unexpected at this time." instead of printing.
  echo   The app closed with an error ^(shown above^).
  echo   Copy the message and send it to Marc.
  echo ============================================================
  echo.
  pause
)
endlocal
