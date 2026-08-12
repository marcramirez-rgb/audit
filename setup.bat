@echo off
setlocal enableextensions
REM pushd (not cd /d) so this also works when double-clicked from a
REM network share (\\server\share\axis_api_testing) -- cd /d leaves the
REM working directory on a UNC path, which several tools called below
REM don't handle; pushd maps a temporary drive letter instead.
pushd "%~dp0"

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
REM installs ship very long internal filenames (over 100 chars below
REM .venv\), so a deep project folder makes pip fail PARTWAY
REM and leave a half-built .venv. Catch it up front instead. The trick:
REM %PROJDIR:~140,1% is the single character at position 140 -- it comes back
REM empty only when the path is 140 chars or shorter.
set "PROJDIR=%~dp0"
if not "%PROJDIR:~140,1%"=="" goto :pathtoolong

set "REQ_CORE=%~dp0requirements-core.txt"
set "SNOWFLAKE_PKG=snowflake-connector-python[secure-local-storage]==4.7.1"
set "PYCHECK_FILE=%TEMP%\lvt_pycheck_%RANDOM%.txt"
set "PF86=%ProgramFiles(x86)%"

REM --- 1. Find a Python interpreter (does NOT rely on PATH) --------------
call :find_python
if defined PYEXE goto :python_found

REM --- 1b. Nothing usable on this machine -- install Python ourselves ---
REM Per-user install (InstallAllUsers=0) needs no admin rights, so this
REM works even on locked-down corporate laptops.
echo No usable Python was found on this computer.
echo Installing Python for your account only (no admin rights needed,
echo does not touch any other app on this computer) ...
echo.
call :bootstrap_python
if not defined PYEXE goto :nopython

:python_found
echo Found Python: %PYEXE%
%PYEXE% --version
echo.
goto :after_python

REM ======================================================================
REM Finds a real, working Python and sets PYEXE. Every candidate is proven
REM by actually running "--version" and checking the output -- never by
REM looking at its file path. (An earlier draft rejected anything whose
REM path contained "WindowsApps" to dodge the Microsoft Store's placeholder
REM stub -- but a *legitimately* Store-installed Python also resolves
REM through that same WindowsApps path once "App execution aliases" are
REM turned on for it, so that draft broke on exactly that machine. A
REM functional check can't make that mistake: if it prints a real version,
REM it works, no matter where the exe lives.)
REM ======================================================================
:find_python
set "PYEXE="

py -3 --version >"%PYCHECK_FILE%" 2>&1
findstr /b /i "Python " "%PYCHECK_FILE%" >nul 2>&1 && set "PYEXE=py -3"

if not defined PYEXE (
  python --version >"%PYCHECK_FILE%" 2>&1
  findstr /b /i "Python " "%PYCHECK_FILE%" >nul 2>&1 && set "PYEXE=python"
)

if not defined PYEXE (
  python3 --version >"%PYCHECK_FILE%" 2>&1
  findstr /b /i "Python " "%PYCHECK_FILE%" >nul 2>&1 && set "PYEXE=python3"
)

REM Microsoft Store install: same functional check, just a fixed path --
REM works whether or not "App execution aliases" happen to be on.
if not defined PYEXE (
  set "STORE_PY=%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe"
  "%STORE_PY%" --version >"%PYCHECK_FILE%" 2>&1
  findstr /b /i "Python " "%PYCHECK_FILE%" >nul 2>&1 && set "PYEXE="%STORE_PY%""
)

REM Registry (covers machines where both PATH and aliases are misconfigured)
if not defined PYEXE (
  reg query "HKCU\Software\Python\PythonCore" /s /v ExecutablePath >"%TEMP%\lvt_pyreg.txt" 2>nul
  for /f "tokens=2,*" %%A in ('findstr /i "ExecutablePath" "%TEMP%\lvt_pyreg.txt"') do if exist "%%B" if not defined PYEXE set "PYEXE="%%B""
)
if not defined PYEXE (
  reg query "HKLM\Software\Python\PythonCore" /s /v ExecutablePath >"%TEMP%\lvt_pyreg.txt" 2>nul
  for /f "tokens=2,*" %%A in ('findstr /i "ExecutablePath" "%TEMP%\lvt_pyreg.txt"') do if exist "%%B" if not defined PYEXE set "PYEXE="%%B""
)
del "%TEMP%\lvt_pyreg.txt" >nul 2>&1

REM Last resort: common install folders, Python 3.8 through 3.15+.
if not defined PYEXE (
  for /d %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python3*"
    "%PROGRAMFILES%\Python3*"
    "%PF86%\Python3*"
    "C:\Python3*"
  ) do if exist "%%D\python.exe" if not defined PYEXE set "PYEXE="%%D\python.exe""
)

del "%PYCHECK_FILE%" >nul 2>&1
exit /b 0

REM ======================================================================
REM Downloads and silently installs Python 3.12 for the current user only,
REM then re-runs find_python so the rest of the script picks it up. Two
REM download methods are tried: curl.exe (built into Windows 10 1803+ and
REM Windows 11) already trusts the Windows certificate store -- the same
REM store corporate SSL-inspection proxies (Zscaler / Netskope) install
REM their CA into -- so it usually just works; PowerShell is the fallback
REM for the rare machine without curl.
REM ======================================================================
:bootstrap_python
set "PYVER=3.12.7"
set "PYINSTALLER=%TEMP%\lvt_python_installer.exe"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"

if exist "%PYINSTALLER%" del "%PYINSTALLER%" >nul 2>&1

echo   [1/2] Downloading Python %PYVER% via curl ...
curl.exe -fsSL -o "%PYINSTALLER%" "%PYURL%" >nul 2>&1

if not exist "%PYINSTALLER%" (
  echo   [2/2] curl unavailable or blocked - retrying via PowerShell ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYINSTALLER%' -UseBasicParsing } catch { exit 1 }"
)

if not exist "%PYINSTALLER%" (
  echo.
  echo   [WARNING] Could not download the Python installer - no network
  echo   path worked. Send this window's text to Marc.
  exit /b 0
)

echo.
echo   Installing (this can take a minute; no window will pop up) ...
"%PYINSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0 SimpleInstall=1

REM The installer process can exit before the files are fully in place
REM under slow AV scanning, so poll for the interpreter instead of
REM assuming the process exiting means it's ready.
set "NEWPY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set /a WAITED=0
:bootstrap_wait
if exist "%NEWPY%" goto :bootstrap_installed
if %WAITED% GEQ 120 goto :bootstrap_timeout
ping -n 3 127.0.0.1 >nul
set /a WAITED+=2
goto :bootstrap_wait

:bootstrap_timeout
echo.
echo   [WARNING] The installer did not finish in time. Send this
echo   window's text to Marc, or re-run setup.bat once it finishes.
exit /b 0

:bootstrap_installed
del "%PYINSTALLER%" >nul 2>&1
echo   Python %PYVER% installed.
echo.
call :find_python
exit /b 0

:after_python

REM --- 2. Create the virtual environment --------------------------------
if exist ".venv\Scripts\python.exe" (
  echo Virtual environment already exists - reusing it.
) else (
  echo Creating virtual environment in .venv ...
  %PYEXE% -m venv .venv
  if errorlevel 1 goto :venvfail
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

REM --- 3. Upgrade pip, setuptools, and wheel (best effort) --------------
echo.
echo Upgrading core setup tools (pip, setuptools, wheel) ...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel >nul 2>&1
if errorlevel 1 "%VENV_PY%" -m pip install --use-feature=truststore --upgrade pip setuptools wheel >nul 2>&1

REM --- 4. Install CORE packages (three-tier fallback) ------------------
REM Installed separately from the Snowflake driver only so the driver gets
REM its own import check + clear status line -- both use the same truststore
REM fallback for corporate SSL.
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
REM Same three-tier fallback as the core packages. IMPORTANT distinction:
REM pip's --use-feature=truststore only changes how pip VERIFIES THE DOWNLOAD
REM (it trusts the Windows cert store, where IT installs the corporate CA). It
REM does NOT change the installed driver's runtime TLS. The old "250003 maximum
REM recursion" bug was the APP calling truststore.inject_into_ssl() at RUNTIME
REM (fleet_catalog.py no longer does that) -- a completely separate thing from
REM this install step. On SSL-inspected laptops the truststore tier is exactly
REM what makes the download succeed, same as the core packages above.
echo.
echo Installing the Snowflake driver for the live Fleet Picker ...
echo.

echo   [1/3] Trying a normal install ...
"%VENV_PY%" -m pip install "%SNOWFLAKE_PKG%"
if not errorlevel 1 goto :snowflake_ok

echo.
echo   [2/3] Normal install failed - retrying via the Windows certificate
echo         store (corporate SSL inspection: Zscaler / Netskope) ...
echo.
"%VENV_PY%" -m pip install --use-feature=truststore "%SNOWFLAKE_PKG%"
if not errorlevel 1 goto :snowflake_ok

echo.
echo   [3/3] Still failing - retrying in trusted-host mode (skips SSL
echo         checking for the PyPI download hosts only) ...
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
echo   just the driver later with this one command:
echo.
echo     ".venv\Scripts\python.exe" -m pip install --use-feature=truststore "%SNOWFLAKE_PKG%"
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
popd
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
echo [ERROR] Could not find or install Python on this computer.
echo.
echo   An automatic install was attempted and failed - see the messages
echo   above for why (usually a blocked download). You can install it
echo   yourself instead:
echo.
echo   1. Install Python 3.11 or newer from:
echo          https://www.python.org/downloads/
echo.
echo   2. On the VERY FIRST installer screen, TICK the box
echo      "Add python.exe to PATH" before clicking Install.
echo.
echo   3. Then double-click setup.bat again.
echo.
echo   If that still doesn't work, copy this window's text and send
echo   it to Marc.
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
popd
pause
exit /b 1
