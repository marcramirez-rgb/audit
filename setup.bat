@echo off
setlocal enableextensions
cd /d "%~dp0"

echo ============================================================
echo   LVT Camera Analytics - First-time setup
echo ============================================================
echo.
echo This sets up everything the app needs in a private folder
echo (.venv) inside this project. It does not touch anything else
echo on your computer. Give it a minute or two the first time.
echo.

REM --- 0. Fail fast if this folder is nested too deep for Windows -------
REM Windows caps a full file path at 260 characters. Some packages this app
REM installs ship very long internal filenames (the anthropic driver has one
REM ~113 chars below .venv\), so a deep project folder makes pip fail PARTWAY
REM and leave a half-built .venv. Catch it up front instead. The trick:
REM %PROJDIR:~140,1% is the single character at position 140 -- it comes back
REM empty only when the path is 140 chars or shorter.
set "PROJDIR=%~dp0"
if not "%PROJDIR:~140,1%"=="" goto :pathtoolong

set "REQ_CORE=%~dp0requirements-core.txt"
set "SNOWFLAKE_PKG=snowflake-connector-python[secure-local-storage]==4.7.1"

REM --- 1. Find a Python interpreter (does NOT rely on PATH) --------------
set "PYEXE="

REM The "py" launcher is installed by python.org even when the
REM "Add python.exe to PATH" box was left unchecked, so try it first.
py -3 --version >nul 2>&1 && set "PYEXE=py -3"

if not defined PYEXE python  --version >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE python3 --version >nul 2>&1 && set "PYEXE=python3"

REM Last resort: look in the usual install locations.
if not defined PYEXE for %%D in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%PROGRAMFILES%\Python313\python.exe"
  "%PROGRAMFILES%\Python312\python.exe"
  "%PROGRAMFILES%\Python311\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
) do if not defined PYEXE if exist "%%~D" set PYEXE="%%~D"

if not defined PYEXE goto :nopython

echo Found Python: %PYEXE%
%PYEXE% --version
echo.

REM --- 2. Create the virtual environment --------------------------------
if exist ".venv\Scripts\python.exe" (
  echo Virtual environment already exists - reusing it.
) else (
  echo Creating virtual environment in .venv ...
  %PYEXE% -m venv .venv
  if errorlevel 1 goto :venvfail
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

REM --- 3. Upgrade pip (best effort; ignore failure) ---------------------
echo.
echo Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
if errorlevel 1 "%VENV_PY%" -m pip install --use-feature=truststore --upgrade pip >nul 2>&1

REM --- 4. Install CORE packages (three-tier fallback) ------------------
REM These are safe to install with truststore. The Snowflake driver is
REM installed separately below because truststore breaks it.
echo.
echo Installing core packages from requirements-core.txt ...
echo.

echo   [1/3] Trying a normal install ...
"%VENV_PY%" -m pip install -r "%REQ_CORE%"
if not errorlevel 1 goto :verify

echo.
echo   [2/3] Normal install failed - this is usually corporate SSL
echo         inspection (Zscaler / Netskope). Retrying using the
echo         Windows certificate store, where IT installs the company CA ...
echo.
"%VENV_PY%" -m pip install --use-feature=truststore -r "%REQ_CORE%"
if not errorlevel 1 goto :verify

echo.
echo   [3/3] Still failing - retrying in trusted-host mode (skips SSL
echo         checking for the PyPI download hosts only) ...
echo.
"%VENV_PY%" -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org -r "%REQ_CORE%"
if not errorlevel 1 goto :verify

goto :installfail

REM --- 5. Verify the core packages actually import (FATAL if not) -------
:verify
echo.
echo Verifying the core install ...
"%VENV_PY%" -c "import customtkinter, PIL, requests, openpyxl, dotenv"
if errorlevel 1 goto :verifyfail

REM --- 6. Install the Snowflake driver (live Fleet Picker) -------------
REM IMPORTANT: never use --use-feature=truststore here. Snowflake's vendored
REM urllib3 recurses with it ("250003: maximum recursion depth exceeded"),
REM which is how this package used to silently fail to install while the app
REM still launched. Its endpoint uses a publicly-trusted cert, so a normal
REM install works; trusted-host is the only fallback we allow.
echo.
echo Installing the Snowflake driver for the live Fleet Picker ...
echo.

echo   [1/2] Trying a normal install ...
"%VENV_PY%" -m pip install "%SNOWFLAKE_PKG%"
if not errorlevel 1 goto :snowflake_ok

echo.
echo   [2/2] Retrying in trusted-host mode (corporate SSL) ...
echo.
"%VENV_PY%" -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org "%SNOWFLAKE_PKG%"
if not errorlevel 1 goto :snowflake_ok

REM Snowflake failed but the app can still run on the cached catalog, so this
REM is a WARNING, not a hard failure.
echo.
echo ------------------------------------------------------------
echo   [WARNING] The Snowflake driver did not install.
echo.
echo   The apps will still run, but the live Fleet Picker will show
echo   "snowflake-connector-python is not installed". You can retry
echo   just the driver later with this one command (do NOT add
echo   truststore to it):
echo.
echo     ".venv\Scripts\python.exe" -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org "%SNOWFLAKE_PKG%"
echo.
echo   If it keeps failing, copy the error above and send it to Marc.
echo ------------------------------------------------------------
set "SNOWFLAKE_STATUS=MISSING - live Fleet Picker unavailable (cached catalog still works)"
goto :done

:snowflake_ok
REM Confirm it actually imports, not just that pip claimed success.
"%VENV_PY%" -c "import snowflake.connector" >nul 2>&1
if errorlevel 1 (
  set "SNOWFLAKE_STATUS=installed but not importable - send the error to Marc"
) else (
  set "SNOWFLAKE_STATUS=installed - live Fleet Picker ready"
)

:done
echo.
echo ============================================================
echo   Setup complete!
echo.
echo   Core packages: OK
echo   Snowflake driver: %SNOWFLAKE_STATUS%
echo.
echo   You can now double-click either of these:
echo       "Run Analytics Writer.bat"
echo       "Run Audit Report.bat"
echo ============================================================
echo.
pause
exit /b 0

REM ---------------------------------------------------------------------
:pathtoolong
echo.
echo [ERROR] This folder is nested too deep for Windows to install into.
echo.
echo   This folder:
echo     %~dp0
echo.
echo   Windows limits a file path to 260 characters, and some packages this
echo   app installs have long internal filenames. Installing here would fail
echo   partway and leave a broken setup.
echo.
echo   FIX: Move the whole "axis_api_testing" folder somewhere shorter --
echo        for example  C:\LVT\axis_api_testing  -- then double-click
echo        setup.bat again from the new location.
echo.
goto :fail

:nopython
echo [ERROR] Could not find Python on this computer.
echo.
echo   1. Install Python 3.11 or newer from:
echo          https://www.python.org/downloads/
echo.
echo   2. On the VERY FIRST installer screen, TICK the box
echo      "Add python.exe to PATH" before clicking Install.
echo.
echo   3. Then double-click setup.bat again.
echo.
goto :fail

:venvfail
echo.
echo [ERROR] Failed to create the virtual environment.
echo   Send this window's text to Marc.
goto :fail

:installfail
echo.
echo [ERROR] Could not install the required packages after 3 attempts.
echo   Copy ALL of the error text above and send it to Marc. The most
echo   useful line is any "SSL", "certificate", or "proxy" message.
goto :fail

:verifyfail
echo.
echo [ERROR] Packages installed but the app still can't import them.
echo   Copy the error above and send it to Marc.
goto :fail

:fail
echo.
echo Setup did not finish. Read the message above, then close this window.
echo.
pause
exit /b 1
