#!/bin/bash
# Build, sign, and notarize Koovu.app + Koovu.dmg for distribution.
# Run on your Mac (Apple Silicon recommended for on-device engine).
set -euo pipefail
cd "$(dirname "$0")"

# --- one-time setup (only needed once per Mac) ---------------------------
# 1. Install your Developer ID Application certificate into Keychain Access
#    (from developer.apple.com/account/resources/certificates).
# 2. Store notarization credentials once:
#      xcrun notarytool store-credentials koovu-notary \
#        --apple-id "you@example.com" --team-id TEAMID --password APP_SPECIFIC_PW
#    (App-specific password from appleid.apple.com; team ID from
#    developer.apple.com/account under Membership.)
# ---------------------------------------------------------------------------

NOTARY_PROFILE="${KOOVU_NOTARY_PROFILE:-koovu-notary}"
SIGN_IDENTITY="${KOOVU_SIGN_IDENTITY:-}"

if [ -z "$SIGN_IDENTITY" ]; then
  SIGN_IDENTITY=$(security find-identity -v -p codesigning \
    | grep "Developer ID Application" | head -1 \
    | sed -E 's/.*"(.*)"/\1/' || true)
fi
if [ -z "$SIGN_IDENTITY" ]; then
  echo "ERROR: no 'Developer ID Application' signing identity found in Keychain." >&2
  echo "       Install your certificate, or set KOOVU_SIGN_IDENTITY explicitly." >&2
  exit 1
fi
echo "→ signing identity: $SIGN_IDENTITY"

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

echo "→ code signing (Developer ID + hardened runtime)"
codesign --force --deep --timestamp --options runtime \
  --entitlements entitlements.plist \
  --sign "$SIGN_IDENTITY" dist/Koovu.app

echo "→ verifying signature"
codesign --verify --deep --strict --verbose=2 dist/Koovu.app
spctl -a -t exec -vv dist/Koovu.app || true   # may still show "not notarized" until stapled below

echo "→ notarizing (this can take a few minutes)…"
rm -f dist/Koovu.zip
ditto -c -k --keepParent dist/Koovu.app dist/Koovu.zip
xcrun notarytool submit dist/Koovu.zip --keychain-profile "$NOTARY_PROFILE" --wait

echo "→ stapling notarization ticket to Koovu.app"
xcrun stapler staple dist/Koovu.app

echo "→ creating Koovu.dmg (drag-to-Applications installer)"
rm -rf build/dmg-staging Koovu.dmg
mkdir -p build/dmg-staging
cp -R dist/Koovu.app build/dmg-staging/
ln -s /Applications build/dmg-staging/Applications
hdiutil create -volname "Koovu" -srcfolder build/dmg-staging \
  -ov -format UDZO Koovu.dmg >/dev/null
rm -rf build/dmg-staging

echo "→ notarizing + stapling Koovu.dmg"
xcrun notarytool submit Koovu.dmg --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple Koovu.dmg

echo ""
echo "✅ Done! Signed + notarized."
echo "   App:  dist/Koovu.app"
echo "   DMG:  Koovu.dmg   ← upload this to your website"
echo ""
echo "Share with anyone — no 'unidentified developer' or malware warnings:"
echo "  1. Download Koovu.dmg → open → drag Koovu to Applications"
echo "  2. Open Koovu normally from Applications (double-click, no right-click needed)"
echo "  3. Grant Microphone + Accessibility when asked"
echo "     (Settings → Privacy & Security → add Koovu to each)"
echo ""
echo "SVG logo for your website: assets/icon.svg"
