@echo off
setlocal
cd /d "%~dp0"

echo.
echo ==============================================
echo   PROOF - Offline Verification Workspace
echo ==============================================
echo.

if exist ".venv\Scripts\python.exe" goto :ready

set "PYTHON_LAUNCHER="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
  if not errorlevel 1 set "PYTHON_LAUNCHER=py -3"
)
if defined PYTHON_LAUNCHER goto :create
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
  if not errorlevel 1 set "PYTHON_LAUNCHER=python"
)
if not defined PYTHON_LAUNCHER (
  echo [ERROR] Python 3.11 or newer was not found through py -3 or python.
  echo If Python is installed, repair its launcher or PATH setting and rerun this file.
  echo Otherwise install it from https://www.python.org/downloads/windows/.
  echo PROOF does not install or replace system Python.
  goto :failed
)

:create
echo [1/3] Creating a virtual environment in this folder...
%PYTHON_LAUNCHER% -m venv .venv
if errorlevel 1 (
  echo [ERROR] Could not create .venv. Check folder write permission and available disk space.
  goto :failed
)

:ready
".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] The existing .venv is broken or uses Python older than 3.11.
  echo Repair its Python installation or extract a fresh copy to another folder.
  goto :failed
)

".venv\Scripts\python.exe" scripts\judge_start.py
if errorlevel 1 goto :failed
goto :end

:failed
echo.
echo PROOF did not start. Read the error above, then press any key to close.
pause >nul
exit /b 1

:end
endlocal
