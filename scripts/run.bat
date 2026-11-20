@echo off
rem Run the DB2 Migration app from source (Windows).
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv ".venv"
    ) else (
        python -m venv ".venv"
    )
    if errorlevel 1 (
        echo ERROR: could not create the virtual environment.
        echo Install Python 3 from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
        exit /b 1
    )
    if not exist ".venv\Scripts\python.exe" (
        echo ERROR: virtual environment was not created correctly. Delete the .venv folder and try again.
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: installing requirements failed.
        exit /b 1
    )
)

echo Starting DB2 Migration UI...
".venv\Scripts\streamlit.exe" run app.py
exit /b %errorlevel%
