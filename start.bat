@echo off
REM Start the transcript proofreading tool and open the browser.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py --open
) else (
  python app.py --open
)
pause
