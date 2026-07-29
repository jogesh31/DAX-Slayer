@echo off
rem Invoked by Power BI Desktop's External Tools ribbon via pbitool.json,
rem with %1=%server% and %2=%database% substituted in by Power BI.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Scripts\pythonw.exe" app.py --server "%~1" --database "%~2"
