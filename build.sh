#!/bin/bash
# Build Koovu.app + Koovu.dmg for sharing with friends.
# Run on your Mac (Apple Silicon recommended for on-device engine).
set -euo pipefail
cd "$(dirname "$0")"

echo "→ setting up build environment"
python3 -m venv venv 2>/dev/null || true
source venv/bin/activate
pip install -q -r requirements.txt pyinstaller

echo "→ building Koovu.app (this takes a few minutes…)"
pyinstaller --noconfirm --windowed --name Koovu \
  --icon assets/appicon.icns \
  --osx-bundle-identifier app.koovu.mac \
  --add-data "assets:assets" \
  --add-data "ui:ui" \
  --hidden-import account \
  --hidden-import HIServices \
  --hidden-import Quartz \
  --hidden-import WebKit \
  --hidden-import AVFoundation \
  --collect-submodules AVFoundation \
  --collect-submodules cryptography \
  koovu.py

# PyInstaller 6.x has no --osx-info-plist; merge our keys after the build.
PLIST="dist/Koovu.app/Contents/Info.plist"
plist_set() {
  local key="$1" type="$2" value="$3"
  if plutil -extract "$key" raw "$PLIST" &>/dev/null; then
    plutil -replace "$key" -"$type" "$value" "$PLIST"
  else
    plutil -insert "$key" -"$type" "$value" "$PLIST"
  fi
}
plist_set LSUIElement bool true
plist_set CFBundleDisplayName string Koovu
plist_set NSMicrophoneUsageDescription string \
  "Koovu needs your microphone to hear your voice for dictation."
plist_set NSAppleEventsUsageDescription string \
  "Koovu may use automation as a fallback to insert transcribed text."
if ! plutil -extract NSMicrophoneUsageDescription raw "$PLIST" &>/dev/null; then
  echo "ERROR: NSMicrophoneUsageDescription missing from Info.plist" >&2
  exit 1
fi
echo "→ Info.plist OK (mic usage description present)"

echo "→ creating Koovu.dmg (drag-to-Applications installer)"
rm -rf build/dmg-staging Koovu.dmg
mkdir -p build/dmg-staging
cp -R dist/Koovu.app build/dmg-staging/
ln -s /Applications build/dmg-staging/Applications
hdiutil create -volname "Koovu" -srcfolder build/dmg-staging \
  -ov -format UDZO Koovu.dmg >/dev/null
rm -rf build/dmg-staging

echo ""
echo "✅ Done!"
echo "   App:  dist/Koovu.app"
echo "   DMG:  Koovu.dmg   ← upload this to your website"
echo ""
echo "Share with friends:"
echo "  1. Download Koovu.dmg → open → drag Koovu to Applications"
echo "  2. First launch: right-click Koovu → Open (unsigned app)"
echo "  3. Grant Microphone + Accessibility when asked"
echo "     (Settings → Privacy & Security → add Koovu to each)"
echo ""
echo "App Store later: needs Apple Developer account (\$99/yr),"
echo "code signing + notarization. DMG is the right path for now."
echo ""
echo "SVG logo for your website: assets/icon.svg"
