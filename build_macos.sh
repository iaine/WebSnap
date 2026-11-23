#!/bin/bash
set -e

# Always run from the folder this script lives in.
cd "$(dirname "$0")"

# Use a virtual environment so pip works with Homebrew/python.org Python.
python3 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
python -m pip install -r requirements.txt pyinstaller

# Install Chromium *inside* the Playwright package rather than in
# ~/Library/Caches, so PyInstaller copies it into WebSnap.app.
# A packaged app always looks there, so users don't need to install anything.
# --only-shell installs just the headless browser the app uses (much smaller).
PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install --only-shell chromium

rm -rf build dist
pyinstaller --noconfirm --windowed --name WebSnap main.py

# Check that the browser really made it into the app, at the path the app
# looks for it. (PyInstaller stores it under a renamed folder and links
# ".local-browsers" to it, so this goes through that link like the app does.)
BROWSERS="dist/WebSnap.app/Contents/Resources/playwright/driver/package/.local-browsers"
if ! ls "$BROWSERS"/chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell >/dev/null 2>&1; then
    echo
    echo "ERROR: Chromium was not bundled into dist/WebSnap.app" >&2
    exit 1
fi

# Zip the app for sharing. ditto keeps the app bundle intact.
# arm64 = Apple Silicon, x86_64 = Intel.
ZIP="dist/WebSnap-macos-$(uname -m).zip"
ditto -c -k --keepParent dist/WebSnap.app "$ZIP"

echo
echo "Built: $(pwd)/dist/WebSnap.app (Chromium included)"
echo "Zip to share: $(pwd)/$ZIP"
