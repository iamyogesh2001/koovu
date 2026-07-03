#!/bin/bash
# Full Koovu reset: quit app, remove installs, restart onboarding.
set -euo pipefail

echo "→ quitting Koovu"
pkill -f "Koovu.app" 2>/dev/null || true
pkill -f koovu.py 2>/dev/null || true
sleep 1

echo "→ removing installed copies"
rm -rf /Applications/Koovu.app
rm -rf "$HOME/Downloads/koovu/dist/Koovu.app"
hdiutil detach /Volumes/Koovu 2>/dev/null || true

echo "→ resetting microphone + accessibility TCC (survives app delete/reinstall)"
tccutil reset Microphone app.koovu.mac 2>/dev/null || true
tccutil reset Accessibility app.koovu.mac 2>/dev/null || true
rm -f "$HOME/.koovu/mic_prompt_pending"

echo "→ resetting config (keeps API keys, restarts onboarding)"
CFG="$HOME/.koovu/config.json"
if [ -f "$CFG" ]; then
  python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.koovu/config.json")
cfg = json.load(open(p))
cfg["onboarded"] = False
cfg["engine"] = "groq_batch"   # reliable in .app; switch to on-device later
json.dump(cfg, open(p, "w"), indent=2)
print("   onboarded=False, engine=groq_batch")
PY
else
  echo "   no config yet — fresh install"
fi

echo ""
echo "✅ Reset done. Now run:"
echo "   cd ~/Downloads/koovu && ./build.sh"
echo "   open Koovu.dmg"
echo "   Drag Koovu → Applications → open from Applications"
