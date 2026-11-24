@echo off
REM Update to the latest version: git pull, reinstall packages if the list changed, run the self-check.
cd /d "%~dp0"
REM cmd reads a parenthesized block into memory before running it,
REM so nothing breaks when git pull replaces this file while it is running.
(
  if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" utils\update.py
  ) else (
    python utils\update.py
  )
  pause
  exit /b
)
