@echo off
rem Registers DAX Slayer as a Power BI Desktop External Tool.
rem Writing to Program Files needs admin rights, so this re-launches itself
rem elevated (one UAC prompt) instead of requiring you to open an admin
rem PowerShell yourself.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo Requesting administrator permission...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "DEST=C:\Program Files (x86)\Common Files\Microsoft Shared\Power BI Desktop\External Tools"
if not exist "%DEST%" mkdir "%DEST%"

rem remove the old registration from before the DAX Slayer rename, if present
if exist "%DEST%\DependencyAnalyzerPro.pbitool.json" del /f /q "%DEST%\DependencyAnalyzerPro.pbitool.json"

copy /Y "%~dp0pbitool.json" "%DEST%\DAXSlayer.pbitool.json"

if errorlevel 1 (
    echo.
    echo Copy failed. Check that Power BI Desktop is installed and try again.
) else (
    echo.
    echo Installed. Restart Power BI Desktop -- "DAX Slayer"
    echo will appear on the External Tools ribbon tab.
)
pause
