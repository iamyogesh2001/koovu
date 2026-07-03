#!/bin/bash
# Deploy Koovu landing page + GitHub release + Supabase backend.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
WEB="$ROOT/website"
DMG="$ROOT/Koovu.dmg"
GH_USER="${GH_USER:-iamyogesh2001}"
REPO="koovu"
TAG="v0.1.0"
PROJECT_REF="kvvdscjpyvodmnuuqwul"

echo "→ 1/4 Supabase (stats + waitlist + edge function)"
if ! supabase projects list &>/dev/null; then
  echo "   Run: supabase login"
  exit 1
fi
supabase link --project-ref "$PROJECT_REF" 2>/dev/null || true
echo "   Running site.sql…"
supabase db query --file "$ROOT/supabase/site.sql" --linked
echo "   Deploying increment-downloads…"
supabase functions deploy increment-downloads --project-ref "$PROJECT_REF" --no-verify-jwt

echo "→ 2/4 GitHub release ($TAG)"
if ! gh auth status &>/dev/null; then
  echo "   Run: gh auth login"
  exit 1
fi
gh repo view "$GH_USER/$REPO" &>/dev/null || \
  gh repo create "$REPO" --public --description "Koovu — voice dictation for every accent"
if [[ ! -f "$DMG" ]]; then
  echo "   Missing $DMG — run ./build.sh first"
  exit 1
fi
if ! git rev-parse --git-dir &>/dev/null; then
  git init -b main
  git remote add origin "https://github.com/$GH_USER/$REPO.git" 2>/dev/null || \
    git remote set-url origin "https://github.com/$GH_USER/$REPO.git"
fi
if ! git rev-parse HEAD &>/dev/null; then
  git add -A
  git commit -m "Initial commit — Koovu beta"
  git push -u origin main
fi
gh release view "$TAG" --repo "$GH_USER/$REPO" &>/dev/null || \
  gh release create "$TAG" "$DMG" \
    --repo "$GH_USER/$REPO" \
    --title "Koovu $TAG" \
    --notes "First public beta. macOS 13+. Right-click → Open on first launch."

echo "→ 3/4 Netlify"
if ! netlify whoami &>/dev/null; then
  echo "   Run: netlify login"
  exit 1
fi
cd "$WEB"
if [[ ! -f .netlify/state.json ]]; then
  netlify deploy --prod --dir . --create-site koovu-app --message "Koovu landing $TAG"
else
  netlify deploy --prod --dir . --message "Koovu landing $TAG"
fi

echo ""
echo "✅ Done"
echo "   DMG:  https://github.com/$GH_USER/$REPO/releases/download/$TAG/Koovu.dmg"
echo "   Site: see Netlify URL above"
