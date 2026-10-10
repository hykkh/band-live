@echo off
rem Manual run with a visible log window. Exits at once if already running (port 7960).
cd /d "C:\H-Programsand-live"
"C:\Python312\python.exe" app.py
exit %errorlevel%
