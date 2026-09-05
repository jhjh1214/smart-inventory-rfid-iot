@echo off
REM Point every attached ESP32 at this laptop on the current network.
REM Safe to run repeatedly. See tools\sync_boards.py for what it changes.
setlocal
set PY=%~dp0tools\esptoolenv\Scripts\python.exe
if not exist "%PY%" (
  echo Toolchain venv missing. Build it first:
  echo   python -m venv tools\esptoolenv
  echo   tools\esptoolenv\Scripts\python -m pip install -r tools\requirements-esp32.txt
  exit /b 1
)
"%PY%" "%~dp0tools\sync_boards.py" %*
echo.
pause
