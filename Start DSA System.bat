@echo off
REM Double-click to start the DSA System and open it in your browser.
cd /d "%~dp0"

REM Start the server in a minimized window (close that window to stop the app).
REM If it is already running, the second copy simply exits and the browser still opens.
start "DSA System Server - close this window to stop" /min "%~dp0..\.venv\Scripts\python.exe" "%~dp0app.py"

REM Give the server a few seconds to start, then open the browser.
timeout /t 4 /nobreak >nul
start "" "http://127.0.0.1:5000"
