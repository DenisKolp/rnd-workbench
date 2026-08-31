@echo off
setlocal
cd /d "%~dp0electron"

if not exist "node_modules\electron\dist\electron.exe" (
  echo Electron dependencies are not installed.
  echo Run: npm install
  pause
  exit /b 1
)

npm start
exit /b %ERRORLEVEL%
