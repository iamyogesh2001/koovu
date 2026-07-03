#!/bin/bash
# Fix Koovu missing from System Settings → Microphone (ghost deny after crashes).
set -euo pipefail
echo "→ quitting Koovu"
pkill -x Koovu 2>/dev/null || true
sleep 1
echo "→ resetting microphone permission for app.koovu.mac"
tccutil reset Microphone app.koovu.mac
echo ""
echo "✅ Done. Now:"
echo "   1. Open Koovu from /Applications (not the DMG)"
echo "   2. Settings → Permissions → Request access (Microphone)"
echo "   3. Tap Allow on the macOS dialog"
echo "   4. Koovu will appear in System Settings → Microphone"
echo ""
open /Applications/Koovu.app 2>/dev/null || open dist/Koovu.app
