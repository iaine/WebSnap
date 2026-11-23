@echo off
setlocal

REM Always run from the folder this script lives in.
cd /d "%~dp0"

REM Use a virtual environment so the build doesn't touch the system Python.
python -m venv .venv 2>nul || py -3 -m venv .venv || goto :fail
call .venv\Scripts\activate.bat || goto :fail

python -m pip install -U pip || goto :fail
python -m pip install -r requirements.txt pyinstaller || goto :fail

REM Install Chromium *inside* the Playwright package so PyInstaller copies
REM it into the app. A packaged app always looks there, so users don't
REM need to install anything. --only-shell = just the headless browser.
set PLAYWRIGHT_BROWSERS_PATH=0
python -m playwright install --only-shell chromium || goto :fail
set PLAYWRIGHT_BROWSERS_PATH=

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
pyinstaller --noconfirm --windowed --name WebSnap main.py || goto :fail

REM Check that the browser really made it into the app.
dir /s /b dist\WebSnap\chrome-headless-shell.exe 2>nul | findstr /i ".local-browsers" >nul
if errorlevel 1 (
    echo ERROR: Chromium was not bundled into dist\WebSnap
    goto :fail
)

REM Zip the app folder for sharing.
powershell -NoProfile -Command "Compress-Archive -Path dist\WebSnap -DestinationPath dist\WebSnap-windows.zip -Force" || goto :fail

echo.
echo Built: %CD%\dist\WebSnap\WebSnap.exe (Chromium included)
echo Zip to share: %CD%\dist\WebSnap-windows.zip
exit /b 0

:fail
echo.
echo Build failed.
exit /b 1
