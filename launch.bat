@echo off
cd /d "%~dp0"
py -3 main.py
if errorlevel 1 (
    echo.
    echo Cinema could not start. If it complains about PIL, install it with:
    echo     py -3 -m pip install -r requirements.txt
    echo.
    pause
)
