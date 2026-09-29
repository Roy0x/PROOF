@echo off
setlocal
cd /d "%~dp0"

echo.
echo ==============================================
echo   PROOF - Verification Engineer
 echo ==============================================
echo.

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set PY=py
) else (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 (
    set PY=python
  ) else (
    echo [ERROR] Python was not found.
    echo Install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo During installation, enable "Add Python to PATH".
    echo.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating local Python environment...
  %PY% -m venv .venv
  if errorlevel 1 goto :failed
) else (
  echo [1/4] Local Python environment already exists.
)

echo [2/4] Installing/checking dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [3/4] Starting PROOF at http://127.0.0.1:8000
echo [4/4] Your browser will open automatically.
echo.
echo KEEP THIS WINDOW OPEN while using PROOF.
echo Press Ctrl+C to stop the server.
echo.

start "" cmd /c "timeout /t 2 /nobreak >nul & start \"\" http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

goto :end

:failed
echo.
echo [ERROR] PROOF could not start. Copy the error shown above and send it to ChatGPT.
echo.
pause
exit /b 1

:end
endlocal
