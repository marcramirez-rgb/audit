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

set "REQ=%~dp0requirements.txt"

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

REM --- 4. Install required packages (three-tier fallback) ---------------
echo.
echo Installing required packages from requirements.txt ...
echo.

echo   [1/3] Trying a normal install ...
"%VENV_PY%" -m pip install -r "%REQ%"
if not errorlevel 1 goto :verify

echo.
echo   [2/3] Normal install failed - this is usually corporate SSL
echo         inspection (Zscaler / Netskope). Retrying using the
echo         Windows certificate store, where IT installs the company CA ...
echo.
"%VENV_PY%" -m pip install --use-feature=truststore -r "%REQ%"
if not errorlevel 1 goto :verify

echo.
echo   [3/3] Still failing - retrying in trusted-host mode (skips SSL
echo         checking for the PyPI download hosts only) ...
echo.
"%VENV_PY%" -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org -r "%REQ%"
if not errorlevel 1 goto :verify

goto :installfail

REM --- 5. Verify the app can actually import its dependencies -----------
:verify
echo.
echo Verifying the install ...
"%VENV_PY%" -c "import customtkinter, PIL, requests, openpyxl, dotenv"
if errorlevel 1 goto :verifyfail

echo.
echo ============================================================
echo   Setup complete!
echo.
echo   You can now double-click either of these:
echo       "Run Analytics Writer.bat"
echo       "Run Audit Report.bat"
echo ============================================================
echo.
pause
exit /b 0

REM ---------------------------------------------------------------------
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
