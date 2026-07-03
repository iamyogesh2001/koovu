# koovu

**Voice → polished text, everywhere on your Mac.**

Tap a hotkey, talk in your natural accent — Indian English, Tamil, Tanglish, whatever — and Koovu types clean, formatted text into any app. Emails come out as emails. Slack messages stay casual. Fillers and "umm no wait" self-corrections disappear.

Free forever. Bring your own [Groq API key](https://console.groq.com/keys) (also free).

## Why

Wispr Flow is great and costs $15/month. Koovu does the core job for $0:

| | Koovu | Wispr Flow |
|---|---|---|
| Price | free (your own Groq key) | $15/mo |
| Indian English | ✅ tuned for it | ✅ |
| Tanglish (Tamil in English letters) | ✅ | ❌ |
| AI cleanup + email formatting | ✅ | ✅ |
| Context aware (knows the app you're in) | ✅ | ✅ |
| Custom vocabulary | ✅ | ✅ |
| Open source | ✅ | ❌ |

## Install

**Option A — download the app** (easiest)

1. Grab `Koovu.dmg` from [Releases](../../releases)
2. Drag Koovu to Applications, then open it normally (double-click — it's signed and notarized, no right-click workaround needed)
3. Grant the three permissions macOS asks for: **Microphone**, **Accessibility**, **Input Monitoring** (System Settings → Privacy & Security → add Koovu to each)
4. Click the Koovu icon in your menu bar → Settings → paste your free Groq API key

**Option B — build from source**

```bash
git clone https://github.com/YOURNAME/koovu.git
cd koovu
./build.sh          # produces dist/Koovu.app
```

**Option C — run with Python** (no app bundle)

```bash
git clone https://github.com/YOURNAME/koovu.git
cd koovu
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 koovu.py
```

## Use

1. Click into any text field — Mail, Slack, Notes, a browser, anywhere
2. **Tap Left Ctrl** (default) → ding → talk
3. **Tap again** → Koovu transcribes, cleans up, and types it where your cursor is

The menu bar icon dances with your voice while recording.

Say things like *"write this as an email"*, *"new paragraph"*, *"make it a bullet list"* — they're treated as commands, not text.

## Settings

Click the menu bar icon → **Settings…**

- **Hotkey** — Left/Right Ctrl, Right Option, Right Cmd, F8, F9
- **Mode** — tap to start/stop, or hold to talk
- **Max seconds** — up to 480 (8 min, Groq's per-request ceiling)
- **Output style** — Auto, or Tanglish (Tamil stays in English letters)
- **Custom vocabulary** — your names & jargon, transcribed correctly
- **AI cleanup** — toggle the LLM pass off for raw transcripts

Config lives at `~/.koovu/config.json`, history at `~/.koovu/history.json`. Nothing else is stored; audio goes to Groq for transcription and is not kept by Koovu.

## Cost

Groq's free tier covers normal personal use. Beyond it: Whisper large-v3 is ~$0.11 per **hour** of audio and the cleanup model costs fractions of a cent. Heavy daily use ≈ $3/mo.

## Roadmap

- [ ] Live streaming text while you talk
- [ ] Accounts, sync, model choice
- [ ] More Indian languages (Hindi/Hinglish, Telugu, Kannada…)
- [x] Signed + notarized builds

## Troubleshooting

- **Hotkey does nothing** → Input Monitoring permission missing for Koovu
- **Nothing pastes** → Accessibility permission missing
- **No sound recorded** → Microphone permission missing
- **Ctrl double-tap opens Apple Dictation** → System Settings → Keyboard → Dictation → change or disable its shortcut
- **Upgrading from an older unsigned build** → macOS ties Mic/Accessibility grants to the app's signature. Run ./reset.sh once after installing the new signed build, then re-grant permissions when asked


MIT license. Built with Groq Whisper large-v3 + Llama 3.3.
