#!/usr/bin/env python3
"""
Koovu — voice to polished text, everywhere on your Mac.

Menu bar app:
  - Global hotkey (default: tap Left Ctrl to start/stop)
  - Audio-reactive menu bar icon while recording
  - Start/stop sounds
  - Groq Whisper large-v3 transcription (Indian English / Tamil / Tanglish)
  - LLM cleanup pass (fillers removed, self-corrections fixed, formatting)
  - Pastes result into whatever app has focus
  - Settings popover (click menu bar icon → native floating panel)
"""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
import warnings
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Harmless shutdown noise from the Hugging Face model downloader (mlx-whisper)
warnings.filterwarnings("ignore", message=".*leaked semaphore.*")

import account

import numpy as np
import requests
import rumps
import sounddevice as sd
from Foundation import NSObject, NSURL, NSURLRequest
from ApplicationServices import (AXUIElementCreateSystemWide,
                                   AXUIElementCopyAttributeValue,
                                   AXUIElementSetAttributeValue,
                                   AXValueCreate, AXValueGetValue,
                                   kAXValueCFRangeType)
from CoreFoundation import CFRangeMake
from WebKit import WKWebView, WKWebViewConfiguration

_AX_FOCUSED = "AXFocusedUIElement"
_AX_VALUE = "AXValue"
_AX_SEL_RANGE = "AXSelectedTextRange"
_AX_SEL_TEXT = "AXSelectedText"
_AX_NCHARS = "AXNumberOfCharacters"

# Native UI (AppKit via PyObjC — ships with rumps)
from AppKit import (NSApplication, NSPanel, NSView, NSColor, NSBezierPath,
                    NSMakeRect, NSBackingStoreBuffered, NSEvent, NSScreen,
                    NSEventMaskLeftMouseUp, NSKeyDownMask, NSKeyUpMask,
                    NSWindowStyleMaskBorderless,
                    NSWindowStyleMaskNonactivatingPanel,
                    NSStatusWindowLevel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorStationary,
                    NSWindowCollectionBehaviorTransient)

APP_NAME = "Koovu"
if getattr(sys, "frozen", False):
    HERE = sys._MEIPASS          # PyInstaller bundle resources
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
UI_DIR = os.path.join(HERE, "ui")
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".koovu")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
LOG_PATH = os.path.join(CONFIG_DIR, "koovu.log")
MIC_FLAG = os.path.join(CONFIG_DIR, "mic_prompt_pending")
MIC_RELAUNCH_FLAG = os.path.join(CONFIG_DIR, ".mic_relaunched")
SETTINGS_PORT = 4739

DEFAULT_CONFIG = {
    "groq_api_key": "",
    "hotkey": "left_ctrl",
    "mode": "toggle",
    "asr_model": "whisper-large-v3",
    "llm_model": "llama-3.3-70b-versatile",
    "language": "en",
    "engine": "groq_batch",          # groq_batch | deepgram_live | local_batch
    "local_model": "mlx-community/whisper-large-v3-turbo",
    "deepgram_api_key": "",
    "deepgram_language": "en",       # nova-2: use "en" (not en-IN)
    "output_style": "auto",          # auto | tanglish
    "custom_vocabulary": [],
    "corrections": [],               # [{"heard": "...", "meant": "..."}]
    "cleanup": True,
    "cleanup_level": "light",        # light | medium | heavy
    "sounds": True,
    "sample_rate": 16000,
    "max_seconds": 480,
    "onboarded": False
}

# macOS virtual key codes for NSEvent global monitors (main-thread hotkeys).
HOTKEY_VK = {
    "left_ctrl": 59,
    "right_ctrl": 62,
    "right_alt": 61,
    "right_cmd": 54,
    "f8": 100,
    "f9": 101,
}

GROQ_ASR_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_LOCAL_MODEL = "mlx-community/whisper-large-v3-turbo"
DEEPGRAM_WS = ("wss://api.deepgram.com/v1/listen"
               "?model=nova-2&encoding=linear16&sample_rate={sr}&channels=1"
               "&punctuate=true&smart_format=true&interim_results=true"
               "&language={lang}")


# ----------------------------------------------------------- live typing ----


def _ax_get(el, attr):
    err, val = AXUIElementCopyAttributeValue(el, attr, None)
    return val if err == 0 else None


def _ax_focused():
    return _ax_get(AXUIElementCreateSystemWide(), _AX_FOCUSED)


def _ax_range(el):
    """Return (location, length) of the selection in a text field."""
    raw = _ax_get(el, _AX_SEL_RANGE)
    if raw is not None:
        err, rng = AXValueGetValue(raw, kAXValueCFRangeType, None)
        if err == 0 and rng is not None:
            return int(rng.location), int(rng.length)
    nchars = _ax_get(el, _AX_NCHARS)
    if nchars is not None:
        end = int(nchars)
        return end, 0
    value = _ax_get(el, _AX_VALUE)
    if value is not None:
        end = len(str(value))
        return end, 0
    return 0, 0


def _ax_set_caret(el, pos):
    ax_range = AXValueCreate(kAXValueCFRangeType, CFRangeMake(int(pos), 0))
    return AXUIElementSetAttributeValue(el, _AX_SEL_RANGE, ax_range) == 0


def _ax_insert_at_cursor(text):
    """Insert text at the caret via Accessibility — no AppleScript needed."""
    if not text:
        return True
    el = _ax_focused()
    if not el:
        return False
    value = _ax_get(el, _AX_VALUE)
    if value is None:
        return False
    current = str(value)
    start, length = _ax_range(el)
    end = start + length
    new_val = current[:start] + text + current[end:]
    if AXUIElementSetAttributeValue(el, _AX_VALUE, new_val) != 0:
        return False
    _ax_set_caret(el, start + len(text))
    return True


def _ax_append_text(text):
    """Append to the focused field (live streaming)."""
    if not text:
        return True
    el = _ax_focused()
    if not el:
        return False
    value = _ax_get(el, _AX_VALUE)
    if value is not None:
        new_val = str(value) + text
        if AXUIElementSetAttributeValue(el, _AX_VALUE, new_val) != 0:
            return False
        _ax_set_caret(el, len(new_val))
        return True
    return _ax_insert_at_cursor(text)


def _ax_backspace(n):
    """Delete n characters before the caret via Accessibility."""
    if n <= 0:
        return True
    el = _ax_focused()
    if not el:
        return False
    value = _ax_get(el, _AX_VALUE)
    if value is not None:
        current = str(value)
        start, length = _ax_range(el)
        if length > 0:
            new_val = current[:start] + current[start + length:]
            if AXUIElementSetAttributeValue(el, _AX_VALUE, new_val) != 0:
                return False
            _ax_set_caret(el, start)
            return True
        delete_from = max(0, start - n)
        new_val = current[:delete_from] + current[start:]
        if AXUIElementSetAttributeValue(el, _AX_VALUE, new_val) != 0:
            return False
        _ax_set_caret(el, delete_from)
        return True
    nchars = _ax_get(el, _AX_NCHARS)
    if nchars is not None:
        end = int(nchars)
        start = max(0, end - n)
        ax_range = AXValueCreate(kAXValueCFRangeType, CFRangeMake(start, n))
        if AXUIElementSetAttributeValue(el, _AX_SEL_RANGE, ax_range) == 0:
            return AXUIElementSetAttributeValue(el, _AX_SEL_TEXT, "") == 0
    return False


def _paste_via_cgevent():
    """Simulate Cmd+V via Quartz — needs Accessibility, not Automation."""
    try:
        from Quartz import (CGEventCreateKeyboardEvent, CGEventPost,
                            CGEventSetFlags, kCGEventFlagMaskCommand,
                            kCGHIDEventTap)
        key_v = 9
        for down in (True, False):
            ev = CGEventCreateKeyboardEvent(None, key_v, down)
            CGEventSetFlags(ev, kCGEventFlagMaskCommand)
            CGEventPost(kCGHIDEventTap, ev)
        return True
    except Exception as e:
        print(f"[koovu] CGEvent paste failed: {e}")
        return False


def type_live(text):
    """Type text into the focused field (Accessibility API only)."""
    if _ax_append_text(text) or _ax_insert_at_cursor(text):
        return


def backspace(n):
    """Delete n characters from the focused field."""
    if n > 0:
        _ax_backspace(n)


def _ax_replace_instant(n, text):
    """Swap last n characters via Accessibility — no selection animation."""
    if n <= 0:
        return False
    el = _ax_focused()
    if not el:
        return False
    value = _ax_get(el, _AX_VALUE)
    if value is not None:
        current = str(value)
        if len(current) >= n:
            err = AXUIElementSetAttributeValue(
                el, _AX_VALUE, current[:-n] + text)
            if err == 0:
                return True
    nchars = _ax_get(el, _AX_NCHARS)
    if nchars is not None:
        end = int(nchars)
        start = max(0, end - n)
        ax_range = AXValueCreate(kAXValueCFRangeType, CFRangeMake(start, n))
        if (AXUIElementSetAttributeValue(el, _AX_SEL_RANGE, ax_range) == 0
                and AXUIElementSetAttributeValue(el, _AX_SEL_TEXT, text) == 0):
            return True
    return False


def replace_typed_text(n, text):
    """Replace last n typed characters instantly (AX first, no AppleScript)."""
    if n <= 0 and not text:
        return
    if n > 0 and _ax_replace_instant(n, text):
        return
    if n <= 0 and text and _ax_insert_at_cursor(text):
        return
    try:
        old = subprocess.run(["pbpaste"], capture_output=True, text=True,
                             timeout=3).stdout
    except Exception:
        old = None
    subprocess.run(["pbcopy"], input=text, text=True)
    time.sleep(0.05)
    if _paste_via_cgevent():
        if old is not None:
            def restore():
                time.sleep(1.2)
                subprocess.run(["pbcopy"], input=old, text=True)
            threading.Thread(target=restore, daemon=True).start()
        return
    print("[koovu] could not insert text — enable Accessibility for Koovu "
          "in Settings → Privacy → Accessibility")


def paste_text(text):
    """Insert transcribed text at the cursor."""
    if _ax_insert_at_cursor(text):
        return
    try:
        old = subprocess.run(["pbpaste"], capture_output=True, text=True,
                             timeout=3).stdout
    except Exception:
        old = None
    subprocess.run(["pbcopy"], input=text, text=True)
    time.sleep(0.1)
    if _paste_via_cgevent():
        if old is not None:
            def restore():
                time.sleep(1.2)
                subprocess.run(["pbcopy"], input=old, text=True)
            threading.Thread(target=restore, daemon=True).start()
        return
    print("[koovu] could not paste text — enable Accessibility for Koovu "
          "in Settings → Privacy → Accessibility")


# ------------------------------------------------------ deepgram streaming --


class DeepgramStreamer:
    """Streams mic audio to Deepgram; types finalized words live into the
    focused field. stop() returns the full raw transcript + chars typed."""

    def __init__(self):
        self.level = 0.0
        self.recording = False
        self.ws = None
        self.stream = None
        self.chunks = []          # finalized transcript chunks
        self.typed_chars = 0
        self.started_at = 0
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._ws_thread = None

    def _connected(self):
        try:
            return (self.ws is not None
                    and getattr(self.ws, "sock", None) is not None
                    and self.ws.sock.connected)
        except Exception:
            return False

    def start(self):
        import websocket
        self.chunks, self.typed_chars = [], 0
        self.level, self.recording = 0.0, True
        self.started_at = time.time()
        self._closed = threading.Event()
        self._ready = threading.Event()
        lang = CFG.get("deepgram_language", "en")
        if lang in ("en-IN", "en_IN"):
            lang = "en"
        url = DEEPGRAM_WS.format(sr=CFG["sample_rate"], lang=lang)

        def on_open(ws):
            print("[koovu] dg connected")
            self._ready.set()

        def on_message(ws, msg):
            try:
                data = json.loads(msg)
                alt = data.get("channel", {}).get("alternatives", [{}])[0]
                text = alt.get("transcript", "")
                if text and data.get("is_final"):
                    self.chunks.append(text)
                    out = text + " "
                    type_live(out)
                    self.typed_chars += len(out)
            except Exception as e:
                print(f"[koovu] dg parse: {e}")

        def on_error(ws, err):
            print(f"[koovu] dg error: {err}")
            self._closed.set()

        def on_close(ws, code, reason):
            print(f"[koovu] dg closed: code={code} reason={reason!r}")
            self.recording = False
            self._closed.set()

        self.ws = websocket.WebSocketApp(
            url,
            header={"Authorization": f"Token {CFG['deepgram_api_key']}"},
            on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close)
        self._ws_thread = threading.Thread(
            target=lambda: self.ws.run_forever(ping_interval=20, ping_timeout=10),
            daemon=True)
        self._ws_thread.start()

        if not self._ready.wait(timeout=8.0):
            self.recording = False
            try:
                self.ws.close()
            except Exception:
                pass
            raise RuntimeError("Deepgram connection timed out")
        if self._closed.is_set() or not self._connected():
            raise RuntimeError("Deepgram connection closed before streaming")

        import websocket as _ws

        def cb(indata, frames, t, status):
            if not self.recording or self._closed.is_set():
                return
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            self.level = min(1.0, rms / 3000.0)
            if not self._connected():
                return
            try:
                self.ws.send(indata.tobytes(), opcode=_ws.ABNF.OPCODE_BINARY)
            except Exception as e:
                print(f"[koovu] dg send: {e}")
                self.recording = False

        self.stream = sd.InputStream(
            samplerate=CFG["sample_rate"], channels=1, dtype="int16",
            callback=cb)
        self.stream.start()
        return True

    def stop(self):
        self.recording = False
        self.level = 0.0
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        try:
            self.ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        # give Deepgram a moment to flush the last finalized chunk
        self._closed.wait(timeout=2.0)
        try:
            self.ws.close()
        except Exception:
            pass
        return " ".join(self.chunks).strip(), self.typed_chars

SOUND_START = "/System/Library/Sounds/Pop.aiff"
SOUND_STOP = "/System/Library/Sounds/Hero.aiff"
SOUND_ERROR = "/System/Library/Sounds/Basso.aiff"

# ------------------------------------------------------------------ config --


def log(msg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def load_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # nova-2 does not support en-IN — migrate saved configs
    if cfg.get("deepgram_language") in ("en-IN", "en_IN"):
        cfg["deepgram_language"] = "en"
    if cfg.get("cleanup_level") not in ("light", "medium", "heavy"):
        cfg["cleanup_level"] = "light"
    env_key = os.environ.get("GROQ_API_KEY", "")
    if env_key and not cfg["groq_api_key"]:
        cfg["groq_api_key"] = env_key
    # On-device MLX does not work in the packaged .app — always use Groq cloud.
    if getattr(sys, "frozen", False) and cfg.get("engine") == "local_batch":
        cfg["engine"] = "groq_batch"
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_history():
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def push_history(text):
    hist = load_history()
    hist.insert(0, {"text": text, "ts": time.strftime("%b %d, %I:%M %p")})
    hist = hist[:25]
    try:
        with open(HISTORY_PATH, "w") as f:
            json.dump(hist, f, indent=2)
    except Exception:
        pass
    return hist


STATS_PATH = os.path.join(CONFIG_DIR, "stats.json")


def load_stats():
    try:
        with open(STATS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def track_words(text):
    stats = load_stats()
    day = time.strftime("%Y-%m-%d")
    d = stats.get(day, {"words": 0, "sessions": 0})
    d["words"] += len(text.split())
    d["sessions"] += 1
    stats[day] = d
    stats = dict(sorted(stats.items())[-90:])   # keep 90 days
    try:
        with open(STATS_PATH, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass


def stats_summary():
    stats = load_stats()
    today = time.strftime("%Y-%m-%d")
    t = stats.get(today, {"words": 0, "sessions": 0})
    total_words = sum(v["words"] for v in stats.values())
    total_sessions = sum(v["sessions"] for v in stats.values())
    # streak: consecutive days with activity ending today/yesterday
    streak = 0
    day = time.time()
    for _ in range(365):
        key = time.strftime("%Y-%m-%d", time.localtime(day))
        if stats.get(key, {}).get("words", 0) > 0:
            streak += 1
            day -= 86400
        else:
            break
    return {"today_words": t["words"], "today_sessions": t["sessions"],
            "total_words": total_words, "total_sessions": total_sessions,
            "streak": streak}


CFG = load_config()

# ------------------------------------------------------------------ sounds --


def play(path):
    if CFG.get("sounds", True) and os.path.exists(path):
        subprocess.Popen(["afplay", path])


# --------------------------------------------------------------- recording --


class Recorder:
    def __init__(self):
        self.q = queue.Queue()
        self.stream = None
        self.frames = []
        self.recording = False
        self.level = 0.0          # 0..1 rms level for icon animation

    def _callback(self, indata, frames, t, status):
        if self.recording:
            self.q.put(indata.copy())
            rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
            self.level = min(1.0, rms / 3000.0)

    def start(self):
        if self.recording:
            return False
        self.frames = []
        while not self.q.empty():
            self.q.get_nowait()
        self.recording = True
        sr = CFG["sample_rate"]
        try:
            self.stream = sd.InputStream(
                samplerate=sr, channels=1, dtype="int16",
                callback=self._callback)
            self.stream.start()
            return True
        except Exception as e:
            self.recording = False
            print(f"[koovu] microphone failed: {e}")
            return False

    def stop(self):
        if not self.recording:
            return None
        self.recording = False
        self.level = 0.0
        time.sleep(0.05)
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
        while not self.q.empty():
            self.frames.append(self.q.get_nowait())
        if not self.frames:
            return None
        audio = np.concatenate(self.frames, axis=0)
        sr = CFG["sample_rate"]
        if len(audio) / sr < 0.4:
            return None
        max_n = int(CFG["max_seconds"]) * sr
        audio = audio[:max_n]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio.tobytes())
        buf.seek(0)
        return buf.read()


# ------------------------------------------------------------ local whisper --


class LocalWhisper:
    """On-device Whisper via MLX (Apple Silicon). No API key, no network
    after the one-time model download (~1.6 GB from Hugging Face)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = "cold"       # cold | downloading | ready | error
        self.error = ""

    def available(self):
        try:
            import mlx_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def _model_repo(self):
        return CFG.get("local_model") or DEFAULT_LOCAL_MODEL

    def _model_cached(self):
        """True if the model files already exist in the HF cache."""
        try:
            from huggingface_hub import try_to_load_from_cache
            repo = self._model_repo()
            hit = try_to_load_from_cache(repo, "config.json")
            return isinstance(hit, str)
        except Exception:
            return False

    def preload(self):
        """Warm up in a background thread: download (if needed) + first
        inference so the real dictation is instant."""
        def _warm():
            with self._lock:
                if self.state in ("ready", "downloading"):
                    return
                self.state = ("downloading" if not self._model_cached()
                              else "cold")
                try:
                    import mlx_whisper
                    silence = np.zeros(16000, dtype=np.float32)
                    mlx_whisper.transcribe(
                        silence, path_or_hf_repo=self._model_repo())
                    self.state = "ready"
                    print("[koovu] local whisper ready")
                except Exception as e:
                    self.state = "error"
                    self.error = str(e)
                    print(f"[koovu] local whisper preload failed: {e}")
        threading.Thread(target=_warm, daemon=True).start()

    def transcribe(self, wav_bytes):
        import mlx_whisper
        with wave.open(io.BytesIO(wav_bytes)) as wf:
            audio = np.frombuffer(wf.readframes(wf.getnframes()),
                                  dtype=np.int16)
        audio = audio.astype(np.float32) / 32768.0
        lang = CFG.get("language") or None
        if CFG.get("output_style") == "tanglish":
            lang = None    # let whisper handle Tamil/English code-switching
        with self._lock:
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self._model_repo(),
                language=lang,
                temperature=0.0,
                initial_prompt=asr_prompt())
        self.state = "ready"
        return result["text"].strip()


LOCAL_ASR = None      # instantiated in main()


# ------------------------------------------------------------------- groq ---


def asr_prompt():
    vocab = ", ".join(CFG["custom_vocabulary"])
    base = "Indian English speaker."
    if CFG.get("output_style") == "tanglish":
        base = ("Speaker mixes Indian English and Tamil. Write Tamil words in "
                "romanized English letters (Tanglish), e.g. 'naan poi varen', "
                "never Tamil script.")
    if vocab:
        base += f" Vocabulary that may appear: {vocab}."
    return base


def transcribe(wav_bytes):
    r = requests.post(
        GROQ_ASR_URL,
        headers={"Authorization": f"Bearer {CFG['groq_api_key']}"},
        files={"file": ("audio.wav", wav_bytes, "audio/wav")},
        data={
            "model": CFG["asr_model"],
            "language": CFG["language"],
            "prompt": asr_prompt(),
            "temperature": 0,
            "response_format": "json",
        },
        timeout=180,
    )
    r.raise_for_status()
    return r.json().get("text", "").strip()


CLEANUP_PROMPTS = {
    "light": """You lightly polish raw voice-dictation. The speaker uses Indian English and may mix in romanized Tamil (Tanglish). Your job is minimal:

1. Add basic punctuation and capitalization only where clearly needed.
2. Remove obvious filler sounds (um, uh, ah) — keep meaningful words like "like" or "actually".
3. Fix only clear speech-to-text typos, using the vocabulary/corrections provided.
4. Resolve obvious self-corrections (e.g. "Tuesday — no Wednesday" → keep Wednesday).
5. DO NOT rewrite sentences, change word choice, formalize tone, or "fix" Indian English phrasing. Preserve the speaker's natural voice and cadence.
6. If Tanglish appears, keep it romanized — never Tamil script, never translate to formal English.
7. Output ONLY the lightly polished text. No explanations.""",

    "medium": """You clean up raw voice-dictation transcripts. The speaker uses Indian English (sometimes mixed with Tamil/Tanglish). Rules:

1. Remove filler words (um, uh, you know) unless they carry meaning.
2. Resolve mid-sentence self-corrections: keep only the final intent.
3. Fix punctuation, capitalization, and light grammar — but keep the speaker's informal Indian English voice.
4. Obey spoken meta-commands: "new paragraph", "bullet list", etc.
5. Chat apps: keep casual. Emails: light structure only if clearly an email.
6. Tanglish stays romanized — never Tamil script, never translate away.
7. Output ONLY the cleaned text. No explanations.""",

    "heavy": """You heavily clean up raw voice-dictation transcripts. The speaker uses Indian English (sometimes mixed with Tamil). Rules:

1. Remove filler words (um, uh, you know, like, actually, basically) unless meaningful.
2. Resolve mid-sentence self-corrections: keep only the final intent.
3. Fix punctuation, capitalization, paragraph breaks. Polish grammar while keeping meaning.
4. Obey spoken meta-commands: "new paragraph", "write this as an email", "make it a bullet list".
5. If clearly an email (or the active app is a mail client), format as one: greeting, body, sign-off.
6. Chat apps (Slack, Discord, Messages, WhatsApp): keep it casual and compact.
7. Tanglish stays romanized — never Tamil script, never translate to English.
8. Output ONLY the cleaned text. No explanations.""",
}


def cleanup_system_prompt():
    level = CFG.get("cleanup_level", "light")
    return CLEANUP_PROMPTS.get(level, CLEANUP_PROMPTS["light"])


def cleanup(raw, app_name):
    vocab = ", ".join(CFG["custom_vocabulary"])
    corr = CFG.get("corrections", [])
    corr_txt = ""
    if corr:
        pairs = "; ".join(f'"{c["heard"]}" means "{c["meant"]}"'
                          for c in corr[:50])
        corr_txt = (f"Known mis-hearings for this speaker (apply if seen): "
                    f"{pairs}\n")
    user_msg = (f"Active app: {app_name or 'unknown'}\n"
                + (f"Known vocabulary: {vocab}\n" if vocab else "")
                + corr_txt
                + f"Raw transcript:\n{raw}")
    r = requests.post(
        GROQ_CHAT_URL,
        headers={"Authorization": f"Bearer {CFG['groq_api_key']}",
                 "Content-Type": "application/json"},
        json={"model": CFG["llm_model"],
              "temperature": 0.1 if CFG.get("cleanup_level") == "light" else 0.2,
              "messages": [{"role": "system", "content": cleanup_system_prompt()},
                           {"role": "user", "content": user_msg}]},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


# ----------------------------------------------------------------- mac io ---


def host_app_name():
    """Name of the app the user should grant permissions to."""
    if getattr(sys, "frozen", False):
        return "Koovu"
    try:
        p = os.getppid()
        for _ in range(6):
            out = subprocess.run(
                ["ps", "-p", str(p), "-o", "comm="],
                capture_output=True, text=True, timeout=2)
            comm = out.stdout.strip()
            if comm and comm.lower() not in (
                    "python", "python3", "zsh", "bash", "sh", "fish"):
                return comm.replace("-", " ").strip()
            pg = subprocess.run(
                ["ps", "-p", str(p), "-o", "ppid="],
                capture_output=True, text=True, timeout=2)
            p = int(pg.stdout.strip())
    except Exception:
        pass
    return "Terminal"


def _mic_auth_status():
    """0=undetermined 1=restricted 2=denied 3=authorized; -1=unknown."""
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        return int(AVCaptureDevice.authorizationStatusForMediaType_(
            AVMediaTypeAudio))
    except Exception:
        return -1


def _mic_authorized():
    return _mic_auth_status() == 3


def _activate_app():
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def request_accessibility():
    """Show the macOS Accessibility prompt so Koovu appears in the list."""
    try:
        import HIServices
        from Foundation import NSDictionary
        opts = NSDictionary.dictionaryWithObject_forKey_(
            True, "AXTrustedCheckOptionPrompt")
        trusted = HIServices.AXIsProcessTrustedWithOptions(opts)
        print(f"[koovu] accessibility prompt shown (trusted={trusted})")
        return trusted
    except Exception as e:
        print(f"[koovu] accessibility request failed: {e}")
        return False


def _reset_mic_tcc():
    try:
        r = subprocess.run(
            ["tccutil", "reset", "Microphone", "app.koovu.mac"],
            capture_output=True, text=True, timeout=10)
        print(f"[koovu] tccutil reset: {r.stdout.strip()}")
        return r.returncode == 0
    except Exception as e:
        print(f"[koovu] tccutil reset failed: {e}")
        return False


def _mic_flag_set():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    open(MIC_FLAG, "a").close()


def _mic_flag_clear():
    try:
        os.remove(MIC_FLAG)
    except FileNotFoundError:
        pass


def _mic_flag_pending():
    return os.path.exists(MIC_FLAG)


def _koovu_app_path():
    if getattr(sys, "frozen", False):
        return os.path.abspath(os.path.join(
            os.path.dirname(sys.executable), "..", ".."))
    return "/Applications/Koovu.app"


def _restart_koovu_for_mic():
    """Single-instance relaunch after tccutil reset (never use open -n)."""
    app_path = _koovu_app_path()
    _mic_flag_set()
    if os.path.isdir(app_path):
        subprocess.Popen(["open", app_path])
    if APP:
        from PyObjCTools.AppHelper import callAfter
        callAfter(APP.on_quit, None)


def _show_mic_prompt(on_granted=None):
    """Continue + macOS Allow dialog. on_granted runs on main thread if allowed."""
    _activate_app()
    if _mic_auth_status() == 3:
        if on_granted and APP:
            from PyObjCTools.AppHelper import callAfter
            callAfter(on_granted)
        return True
    try:
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Allow microphone for dictation")
        alert.setInformativeText_("Tap Allow on the next macOS dialog.")
        alert.addButtonWithTitle_("Continue")
        alert.addButtonWithTitle_("Not Now")
        if alert.runModal() != 1000:
            return False
        _activate_app()
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        def _done(ok):
            print(f"[koovu] microphone: {'granted' if ok else 'denied'}")
            log(f"microphone prompt: {'granted' if ok else 'denied'}")
            _mic_flag_clear()
            try:
                os.remove(MIC_RELAUNCH_FLAG)
            except FileNotFoundError:
                pass
            if ok:
                threading.Thread(target=_mic_warmup_session,
                                 daemon=True).start()
                if on_granted and APP:
                    from PyObjCTools.AppHelper import callAfter
                    callAfter(on_granted)
            elif APP:
                from PyObjCTools.AppHelper import callAfter
                callAfter(APP._on_mic_denied)

        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio, _done)
        return True
    except Exception as e:
        print(f"[koovu] microphone prompt failed: {e}")
        return False


def _request_microphone_on_main():
    status = _mic_auth_status()
    print(f"[koovu] microphone auth status: {status}")
    if status == 3:
        return
    if status in (2, 1):
        _reset_mic_tcc()
    _show_mic_prompt()


def _handle_mic_on_startup():
    """Fix ghost mic deny: reset → relaunch once → show Allow dialog."""
    status = _mic_auth_status()
    if status == 3:
        _mic_flag_clear()
        try:
            os.remove(MIC_RELAUNCH_FLAG)
        except FileNotFoundError:
            pass
        return
    if status in (2, 1) and not os.path.exists(MIC_RELAUNCH_FLAG):
        print("[koovu] mic ghost deny — resetting and relaunching once")
        _reset_mic_tcc()
        open(MIC_RELAUNCH_FLAG, "w").close()
        app_path = _koovu_app_path()
        if os.path.isdir(app_path):
            subprocess.Popen(["open", app_path])
        if APP:
            from PyObjCTools.AppHelper import callAfter
            callAfter(APP.on_quit, None)
        return
    if os.path.exists(MIC_RELAUNCH_FLAG):
        try:
            os.remove(MIC_RELAUNCH_FLAG)
        except FileNotFoundError:
            pass

    def _go():
        from PyObjCTools.AppHelper import callAfter
        callAfter(_show_mic_prompt)

    threading.Timer(1.5, _go).start()


def _mic_warmup_session():
    """Open the mic briefly after grant — registers with TCC."""
    try:
        from AVFoundation import (AVCaptureDevice, AVMediaTypeAudio,
                                  AVCaptureSession, AVCaptureDeviceInput)
        device = AVCaptureDevice.defaultDeviceWithMediaType_(AVMediaTypeAudio)
        if device is None:
            return
        session = AVCaptureSession.alloc().init()
        inp, _err = AVCaptureDeviceInput.deviceInputWithDevice_error_(
            device, None)
        if inp is not None and session.canAddInput_(inp):
            session.addInput_(inp)
            session.startRunning()
            time.sleep(0.25)
            session.stopRunning()
    except Exception as e:
        print(f"[koovu] mic warmup: {e}")
    try:
        with sd.InputStream(channels=1, samplerate=16000, blocksize=256):
            time.sleep(0.15)
    except Exception as e:
        print(f"[koovu] microphone probe: {e}")


def request_microphone():
    """Trigger the macOS Microphone prompt so Koovu appears in the list."""
    from PyObjCTools.AppHelper import callAfter
    callAfter(_request_microphone_on_main)


def request_macos_permissions():
    """Ask macOS to list Koovu under Accessibility (not microphone)."""
    request_accessibility()
    if APP:
        from PyObjCTools.AppHelper import callAfter
        callAfter(APP.start_listener)


def permissions_status():
    """Best-effort check of macOS privacy gates Koovu needs."""
    ax = False
    try:
        import HIServices
        ax = bool(HIServices.AXIsProcessTrusted())
    except Exception:
        pass
    auto = False
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to return "ok"'],
            capture_output=True, text=True, timeout=5)
        auto = r.returncode == 0 and "ok" in (r.stdout or "")
    except Exception:
        pass
    hotkey = bool(APP and getattr(APP, "_hotkey", None) and APP._hotkey.ok)
    mic_status = _mic_auth_status()
    return {"accessibility": ax, "automation": auto,
            "microphone": mic_status == 3,
            "microphone_status": mic_status,
            "hotkey": hotkey,
            "host_app": host_app_name()}


def open_privacy_pane(pane):
    panes = {
        "mic": "com.apple.settings.PrivacySecurity.extension?Privacy_Microphone",
        "ax": "com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
        "input": "com.apple.settings.PrivacySecurity.extension?Privacy_ListenEvent",
        "automation": "com.apple.settings.PrivacySecurity.extension?Privacy_Automation",
    }
    legacy = {
        "mic": "com.apple.preference.security?Privacy_Microphone",
        "ax": "com.apple.preference.security?Privacy_Accessibility",
        "input": "com.apple.preference.security?Privacy_ListenEvent",
        "automation": "com.apple.preference.security?Privacy_Automation",
    }
    key = panes.get(pane) or legacy.get(pane)
    if key:
        subprocess.Popen(["open", f"x-apple.systempreferences:{key}"])


def frontmost_app():
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first '
             'process whose frontmost is true'],
            capture_output=True, text=True, timeout=3)
        return out.stdout.strip()
    except Exception:
        return ""


# --------------------------------------------------------- settings server --


class SettingsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send(self, code, body, ctype="text/html"):
        data = body.encode() if isinstance(body, str) else body
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path in ("/", "/settings"):
            try:
                with open(os.path.join(UI_DIR, "settings.html")) as f:
                    html = f.read()
            except Exception as e:
                return self._send(500, f"UI missing: {e}")
            payload = {"config": {**CFG,
                                  "groq_api_key_set": bool(CFG["groq_api_key"]),
                                  "deepgram_key_set": bool(CFG.get("deepgram_api_key"))},
                       "local_asr": {
                           "available": bool(LOCAL_ASR and LOCAL_ASR.available()),
                           "state": LOCAL_ASR.state if LOCAL_ASR else "cold"},
                       "history": load_history(),
                       "stats": stats_summary(),
                       "version": "0.4.0"}
            html = html.replace("__KOOVU_STATE__", json.dumps(payload))
            return self._send(200, html)
        if self.path == "/onboarding":
            try:
                with open(os.path.join(UI_DIR, "onboarding.html")) as f:
                    html = f.read()
            except Exception as e:
                return self._send(500, f"UI missing: {e}")
            payload = {"config": {**CFG,
                                  "groq_api_key_set": bool(CFG["groq_api_key"]),
                                  "deepgram_key_set": bool(CFG.get("deepgram_api_key"))}}
            html = html.replace("__KOOVU_STATE__", json.dumps(payload))
            return self._send(200, html)
        if self.path.startswith("/auth-callback"):
            try:
                with open(os.path.join(UI_DIR, "auth.html")) as f:
                    return self._send(200, f.read())
            except Exception as e:
                return self._send(500, f"UI missing: {e}")
        if self.path == "/account/status":
            return self._send(200, json.dumps(account.status()),
                              "application/json")
        if self.path == "/permissions/status":
            return self._send(200, json.dumps(permissions_status()),
                              "application/json")
        if self.path == "/state":
            return self._send(200, json.dumps(
                {"config": CFG, "history": load_history(),
                 "local_asr": {
                     "available": bool(LOCAL_ASR and LOCAL_ASR.available()),
                     "state": LOCAL_ASR.state if LOCAL_ASR else "cold"}}),
                "application/json")
        self._send(404, "not found")

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return None

    def do_POST(self):
        if self.path == "/save":
            data = self._body()
            if data is None:
                return self._send(400, '{"ok":false}', "application/json")
            allowed = ["groq_api_key", "hotkey", "mode", "language",
                       "output_style", "custom_vocabulary", "cleanup",
                       "cleanup_level", "sounds", "max_seconds", "llm_model",
                       "corrections", "engine", "deepgram_api_key",
                       "deepgram_language", "local_model"]
            for k in allowed:
                if k in data:
                    CFG[k] = data[k]
            if isinstance(CFG.get("corrections"), list):
                CFG["corrections"] = [
                    {"heard": str(c.get("heard", "")).strip(),
                     "meant": str(c.get("meant", "")).strip()}
                    for c in CFG["corrections"]
                    if isinstance(c, dict) and str(c.get("heard", "")).strip()
                    and str(c.get("meant", "")).strip()][:100]
            if isinstance(CFG.get("custom_vocabulary"), str):
                CFG["custom_vocabulary"] = [
                    w.strip() for w in CFG["custom_vocabulary"].split(",")
                    if w.strip()]
            if CFG.get("cleanup_level") not in ("light", "medium", "heavy"):
                CFG["cleanup_level"] = "light"
            try:
                CFG["max_seconds"] = max(10, min(480, int(CFG["max_seconds"])))
            except Exception:
                CFG["max_seconds"] = 480
            save_config(CFG)
            if APP:
                # AppKit is main-thread-only; calling apply_config from this
                # HTTP thread crashes with SIGTRAP (menu item title update).
                from PyObjCTools.AppHelper import callAfter
                callAfter(APP.apply_config)
            account.push_async(CFG)   # no-op if signed out
            return self._send(200, '{"ok":true}', "application/json")
        if self.path == "/account/signup":
            data = self._body() or {}
            profile = {"first_name": str(data.get("first_name", "")).strip(),
                       "last_name": str(data.get("last_name", "")).strip(),
                       "phone": str(data.get("phone", "")).strip()}
            res = account.sign_up(data.get("email", "").strip(),
                                  data.get("password", ""), profile)
            if res.get("ok") and not res.get("needs_confirmation"):
                _apply_remote_or_push()
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/account/signin":
            data = self._body() or {}
            res = account.sign_in(data.get("email", "").strip(),
                                  data.get("password", ""))
            if res.get("ok"):
                res["synced"] = _apply_remote_or_push()
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/account/signout":
            return self._send(200, json.dumps(account.sign_out()),
                              "application/json")
        if self.path == "/account/recover":
            data = self._body() or {}
            res = account.recover(data.get("email", "").strip())
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/account/sync":
            res = account.push(CFG)
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/account/set-password":
            data = self._body() or {}
            res = account.set_password(data.get("access_token", ""),
                                       data.get("refresh_token", ""),
                                       data.get("password", ""))
            if res.get("ok"):
                # this device may hold API keys locally — re-push under new key
                account.push_async(CFG)
            return self._send(200, json.dumps(res), "application/json")
        if self.path == "/quit":
            if APP:
                from PyObjCTools.AppHelper import callAfter
                callAfter(APP.on_quit, None)
            return self._send(200, '{"ok":true}', "application/json")
        if self.path == "/onboarding-complete":
            CFG["onboarded"] = True
            save_config(CFG)
            if APP:
                from PyObjCTools.AppHelper import callAfter
                callAfter(APP.close_onboarding)
            return self._send(200, '{"ok":true}', "application/json")
        if self.path == "/permissions/status":
            return self._send(200, json.dumps(permissions_status()),
                              "application/json")
        if self.path == "/permissions/request":
            data = self._body() or {}
            which = data.get("which", "all")
            from PyObjCTools.AppHelper import callAfter
            if which == "microphone":
                callAfter(_request_microphone_on_main)
            elif which == "reset-microphone":
                _reset_mic_tcc()
                from PyObjCTools.AppHelper import callAfter
                callAfter(_show_mic_prompt)
                return self._send(200, '{"ok":true,"reset":true}',
                                  "application/json")
            elif which == "accessibility":
                callAfter(request_accessibility)
            elif APP:
                callAfter(request_macos_permissions)
            else:
                request_macos_permissions()
            return self._send(200, '{"ok":true}', "application/json")
        if self.path == "/dictate/toggle":
            if APP:
                from PyObjCTools.AppHelper import callAfter
                if APP.state == "recording":
                    callAfter(APP.end)
                else:
                    callAfter(APP.begin)
            return self._send(200, '{"ok":true}', "application/json")
        if self.path == "/open-pane":
            n = int(self.headers.get("Content-Length", 0))
            try:
                pane = json.loads(self.rfile.read(n)).get("pane", "")
            except Exception:
                pane = ""
            open_privacy_pane(pane)
            return self._send(200, '{"ok":true}', "application/json")
        if self.path == "/open-url":
            data = self._body() or {}
            url = str(data.get("url", ""))
            if url.startswith(("https://", "http://")):
                subprocess.Popen(["open", url])
            return self._send(200, '{"ok":true}', "application/json")
        self._send(404, "not found")


def _apply_remote_or_push():
    """After sign-in: pull remote settings if they exist, else upload local.
    Returns 'pulled', 'pushed' or 'error'."""
    res = account.pull()
    if not res.get("ok"):
        return "error"
    if res.get("settings") is None:
        account.push(CFG)
        return "pushed"
    for k, v in res["settings"].items():
        if k in account.SYNC_FIELDS and v is not None:
            CFG[k] = v
    keys = res.get("keys")
    if keys:
        if keys.get("groq"):
            CFG["groq_api_key"] = keys["groq"]
        if keys.get("deepgram"):
            CFG["deepgram_api_key"] = keys["deepgram"]
    save_config(CFG)
    if APP:
        from PyObjCTools.AppHelper import callAfter
        callAfter(APP.apply_config)
    return "pulled"


def start_settings_server():
    srv = ThreadingHTTPServer(("127.0.0.1", SETTINGS_PORT), SettingsHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


class _KeyPanel(NSPanel):
    """Borderless NSPanel that can become key. Without this override a
    borderless panel can never take keyboard focus, and on macOS 26
    makeKeyAndOrderFront_ can silently fail to display it at all."""

    def canBecomeKeyWindow(self):
        return True


class _StatusBarTarget(NSObject):
    """Receives clicks on the menu bar icon (no dropdown menu)."""

    def statusBarClick_(self, sender):
        print("[koovu] menu bar icon clicked")
        if APP:
            from PyObjCTools.AppHelper import callAfter
            callAfter(APP.toggle_settings_popover)


class _WebDebug(NSObject):
    """Logs WKWebView load results so blank panels are diagnosable."""

    def webView_didFinishNavigation_(self, wv, nav):
        print("[koovu] web view loaded ok")

    def webView_didFailProvisionalNavigation_withError_(self, wv, nav, err):
        print(f"[koovu] web view FAILED to load: {err.localizedDescription()}")

    def webView_didFailNavigation_withError_(self, wv, nav, err):
        print(f"[koovu] web view load error: {err.localizedDescription()}")

    def webViewWebContentProcessDidTerminate_(self, wv):
        print("[koovu] web view content process crashed")


_WEB_DEBUG = None


def _web_debug():
    global _WEB_DEBUG
    if _WEB_DEBUG is None:
        _WEB_DEBUG = _WebDebug.alloc().init()
    return _WEB_DEBUG


class SettingsPopover:
    """Native floating panel with embedded WKWebView — WiFi-panel style."""

    WIDTH, HEIGHT = 420, 700

    def __init__(self):
        self.panel = None
        self.web = None
        self.visible = False
        self._monitor = None
        self._click_target = None

    def _anchor(self):
        """Screen rect of the menu bar status item button."""
        try:
            item = APP._nsapp.nsstatusitem
            btn = item.button()
            if btn and btn.window():
                return btn.window().convertRect_toScreen_(btn.frame())
        except Exception:
            pass
        f = NSScreen.mainScreen().frame()
        return NSMakeRect(f.size.width - 48, f.size.height - 28, 24, 24)

    def _position(self):
        anchor = self._anchor()
        x = anchor.origin.x + anchor.size.width / 2 - self.WIDTH / 2
        y = anchor.origin.y - self.HEIGHT - 6
        screen = NSScreen.mainScreen().frame()
        x = max(screen.origin.x + 8,
                min(x, screen.origin.x + screen.size.width - self.WIDTH - 8))
        y = max(screen.origin.y + 8, y)
        return (x, y)

    def _ensure(self):
        if self.panel:
            return
        rect = NSMakeRect(0, 0, self.WIDTH, self.HEIGHT)
        style = NSWindowStyleMaskBorderless
        p = _KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        # Opaque black: even if the web view fails to render, the panel is
        # visible instead of a fully transparent (invisible) window.
        p.setOpaque_(True)
        p.setBackgroundColor_(NSColor.blackColor())
        p.setHasShadow_(True)
        p.setBecomesKeyOnlyIfNeeded_(False)
        # THE fix: NSPanel auto-hides when the app deactivates, and Koovu
        # deactivates instantly (user is always in another app). This kept
        # every panel invisible.
        p.setHidesOnDeactivate_(False)
        p.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorTransient)
        # Set level LAST — setFloatingPanel_ resets it to floating (3);
        # the bubble works at NSStatusWindowLevel (25), so match it.
        p.setLevel_(NSStatusWindowLevel)
        cfg = WKWebViewConfiguration.alloc().init()
        web = WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        web.setNavigationDelegate_(_web_debug())
        p.setContentView_(web)
        url = NSURL.URLWithString_(f"http://127.0.0.1:{SETTINGS_PORT}/")
        web.loadRequest_(NSURLRequest.requestWithURL_(url))
        self.panel, self.web = p, web

    def _reload(self):
        if not self.web:
            return
        url = NSURL.URLWithString_(f"http://127.0.0.1:{SETTINGS_PORT}/")
        self.web.loadRequest_(NSURLRequest.requestWithURL_(url))

    def _start_monitor(self):
        if self._monitor:
            return

        def outside(_event):
            if not self.visible or not self.panel:
                return
            try:
                pt = NSEvent.mouseLocation()
                f = self.panel.frame()
                inside = (f.origin.x <= pt.x <= f.origin.x + f.size.width
                          and f.origin.y <= pt.y <= f.origin.y + f.size.height)
                if inside:
                    return
                # status bar icon click is handled by statusBarClick:
                anchor = self._anchor()
                icon = NSMakeRect(anchor.origin.x - 6, anchor.origin.y - 6,
                                  anchor.size.width + 12, anchor.size.height + 12)
                on_icon = (icon.origin.x <= pt.x <= icon.origin.x + icon.size.width
                           and icon.origin.y <= pt.y <= icon.origin.y + icon.size.height)
                if not on_icon:
                    self.hide()
            except Exception:
                self.hide()

        mask = (1 << 1) | (1 << 3)   # LeftMouseDown | RightMouseDown
        self._monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask, outside)

    def _stop_monitor(self):
        if self._monitor:
            try:
                NSEvent.removeMonitor_(self._monitor)
            except Exception:
                pass
            self._monitor = None

    def show(self):
        try:
            self._ensure()
            self.panel.setFrameOrigin_(self._position())
            self.panel.orderFrontRegardless()
            self.panel.makeKeyAndOrderFront_(None)
            self.visible = True
            self._start_monitor()
            f = self.panel.frame()
            print(f"[koovu] settings panel shown at "
                  f"({int(f.origin.x)},{int(f.origin.y)}) "
                  f"onscreen={bool(self.panel.isVisible())}")
        except Exception as e:
            print(f"[koovu] settings popover: {e}")

    def hide(self):
        try:
            if self.panel:
                self.panel.resignKeyWindow()
                self.panel.orderOut_(None)
        except Exception:
            pass
        self.visible = False
        self._stop_monitor()

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def attach_status_item(self, status_item):
        """Replace dropdown menu with click-to-toggle popover."""
        if not self._click_target:
            self._click_target = _StatusBarTarget.alloc().init()
        status_item.setMenu_(None)
        btn = status_item.button()
        if btn:
            btn.setTarget_(self._click_target)
            btn.setAction_("statusBarClick:")
            # Mouse-up ONLY. Down|up made the panel toggle twice per click
            # (open on press, close on release) — looked like a dead icon.
            btn.sendActionOn_(NSEventMaskLeftMouseUp)
            btn.setEnabled_(True)
        status_item.setEnabled_(True)


# Startup (status-bar hook + onboarding) happens from tick_icon — the rumps
# timer is the only mechanism proven to fire reliably after the loop starts.


class OnboardingPanel:
    """Centered first-run setup window (VLC-style step wizard)."""

    WIDTH, HEIGHT = 560, 640

    def __init__(self):
        self.panel = None
        self.visible = False

    def _ensure(self):
        if self.panel:
            return
        rect = NSMakeRect(0, 0, self.WIDTH, self.HEIGHT)
        style = NSWindowStyleMaskBorderless
        p = _KeyPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        p.setOpaque_(True)
        p.setBackgroundColor_(NSColor.blackColor())
        p.setHasShadow_(True)
        p.setBecomesKeyOnlyIfNeeded_(False)
        p.setHidesOnDeactivate_(False)   # never vanish when app loses focus
        p.setLevel_(NSStatusWindowLevel)
        cfg = WKWebViewConfiguration.alloc().init()
        web = WKWebView.alloc().initWithFrame_configuration_(rect, cfg)
        web.setNavigationDelegate_(_web_debug())
        url = NSURL.URLWithString_(
            f"http://127.0.0.1:{SETTINGS_PORT}/onboarding")
        web.loadRequest_(NSURLRequest.requestWithURL_(url))
        p.setContentView_(web)
        screen = NSScreen.mainScreen().frame()
        x = screen.origin.x + (screen.size.width - self.WIDTH) / 2
        y = screen.origin.y + (screen.size.height - self.HEIGHT) / 2 + 40
        p.setFrameOrigin_((x, y))
        self.panel = p

    def show(self):
        try:
            self._ensure()
            self.panel.orderFrontRegardless()
            self.panel.makeKeyAndOrderFront_(None)
            self.visible = True
            f = self.panel.frame()
            print(f"[koovu] onboarding panel shown at "
                  f"({int(f.origin.x)},{int(f.origin.y)}) "
                  f"onscreen={bool(self.panel.isVisible())}")
        except Exception as e:
            print(f"[koovu] onboarding panel: {e}")

    def hide(self):
        try:
            if self.panel:
                self.panel.orderOut_(None)
        except Exception:
            pass
        self.visible = False


# --------------------------------------------------------------- bubble -----


class _BarsView(NSView):
    """Draws the 3 Koovu bars on a dark rounded background."""
    heights = [10.0, 18.0, 13.0]

    def drawRect_(self, rect):
        b = self.bounds()
        NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.08, 0.08, 0.11, 0.95).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, 10, 10).fill()
        NSColor.colorWithCalibratedRed_green_blue_alpha_(
            0.98, 0.98, 0.98, 1.0).setFill()
        W, gap, n = 8.0, 5.0, 3
        total = n * W + (n - 1) * gap
        x0 = (b.size.width - total) / 2
        H = b.size.height
        for i in range(n):
            h = float(self.heights[i])
            x = x0 + i * (W + gap)
            y = (H - h) / 2
            NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, y, W, h), W / 2, W / 2).fill()


class RecordingBubble:
    """Native floating panel near the cursor. All calls MUST happen on the
    main thread (they do: driven by the rumps Timer in KoovuApp)."""

    def __init__(self):
        self.panel = None
        self.view = None
        self.visible = False

    def _ensure(self):
        if self.panel:
            return
        rect = NSMakeRect(0, 0, 56, 40)
        style = (NSWindowStyleMaskBorderless
                 | NSWindowStyleMaskNonactivatingPanel)
        p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        p.setLevel_(NSStatusWindowLevel)          # floats above everything
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(True)
        p.setIgnoresMouseEvents_(True)            # never steals clicks
        p.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary)
        v = _BarsView.alloc().initWithFrame_(rect)
        p.setContentView_(v)
        self.panel, self.view = p, v

    def show(self):
        try:
            self._ensure()
            loc = NSEvent.mouseLocation()         # bottom-left origin
            self.panel.setFrameOrigin_((loc.x + 14, loc.y + 14))
            self.panel.orderFrontRegardless()     # no focus steal
            self.visible = True
        except Exception:
            pass

    def tick(self, level):
        if not self.visible or not self.view:
            return
        try:
            t = time.time()
            base = 8 + level * 20
            hs = []
            for i in range(3):
                h = base * (0.55 + 0.45 * abs(np.sin(t * 6 + i * 0.9)))
                hs.append(max(6.0, min(30.0, h)))
            self.view.heights = hs
            self.view.setNeedsDisplay_(True)
        except Exception:
            pass

    def hide(self):
        try:
            if self.panel:
                self.panel.orderOut_(None)
        except Exception:
            pass
        self.visible = False


# ------------------------------------------------------------------- app ----

APP = None


class HotkeyMonitor:
    """Global hotkey via NSEvent — must run on the AppKit main thread."""

    def __init__(self, app):
        self.app = app
        self._monitors = []
        self.ok = False

    def stop(self):
        for mon in self._monitors:
            try:
                NSEvent.removeMonitor_(mon)
            except Exception:
                pass
        self._monitors = []
        self.ok = False

    def start(self, hotkey_id, mode):
        self.stop()
        try:
            import HIServices
            if not HIServices.AXIsProcessTrusted():
                print("[koovu] hotkey: accessibility not granted")
                return False
        except Exception as e:
            print(f"[koovu] hotkey: trust check failed: {e}")
            return False

        vk = HOTKEY_VK.get(hotkey_id, 59)

        def on_down(event):
            try:
                if event.keyCode() != vk or event.isARepeat():
                    return
                log(f"hotkey down ({hotkey_id})")
                if mode == "hold":
                    if not self.app.rec.recording:
                        self.app.begin()
                elif self.app.toggle_on:
                    self.app.toggle_on = False
                    self.app.end()
                else:
                    self.app.toggle_on = True
                    self.app.begin()
            except Exception as e:
                print(f"[koovu] hotkey down: {e}")

        def on_up(event):
            try:
                if event.keyCode() != vk:
                    return
                if mode == "hold" and self.app.rec.recording:
                    self.app.end()
            except Exception as e:
                print(f"[koovu] hotkey up: {e}")

        down = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask, on_down)
        up = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSKeyUpMask, on_up)
        if down is None or up is None:
            print("[koovu] hotkey: global monitor failed (accessibility?)")
            return False
        self._monitors = [down, up]
        self.ok = True
        print(f"[koovu] hotkey listening ({hotkey_id})")
        return True


class KoovuApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, icon=os.path.join(ASSETS, "idle.png"),
                         quit_button=None, template=True)
        self.rec = Recorder()
        self.state = "idle"           # idle | recording | processing
        self.toggle_on = False
        self._hotkey = None
        self.status_item = rumps.MenuItem("Status: idle")
        self.hint_item = rumps.MenuItem(self._hint())
        self.menu = [
            self.status_item,
            self.hint_item,
            None,
            rumps.MenuItem("Settings…", callback=self.on_settings),
            rumps.MenuItem("Copy last transcription",
                           callback=self.on_copy_last),
            rumps.MenuItem("Start dictation", callback=self.on_dictate),
            None,
            rumps.MenuItem("Quit Koovu", callback=self.on_quit),
        ]
        self.bubble = RecordingBubble()
        self.settings = SettingsPopover()
        self.onboarding = OnboardingPanel()
        self._ticks = 0
        self._startup_done = False
        self.anim = rumps.Timer(self.tick_icon, 0.12)
        self.anim.start()
        self.start_listener()

    def toggle_settings_popover(self):
        if self.onboarding.visible:
            self.onboarding.hide()
        self.settings.toggle()

    def close_onboarding(self):
        self.onboarding.hide()

    # ---- config / listener

    def _hint(self):
        key = CFG["hotkey"].replace("_", " ")
        verb = "tap" if CFG["mode"] == "toggle" else "hold"
        return f"Hotkey: {verb} {key}"

    def apply_config(self):
        self.hint_item.title = self._hint()
        self.start_listener()
        if (CFG.get("engine") == "local_batch" and LOCAL_ASR
                and LOCAL_ASR.available() and LOCAL_ASR.state != "ready"):
            LOCAL_ASR.preload()

    def start_listener(self):
        from PyObjCTools.AppHelper import callAfter

        def _install():
            if self._hotkey is None:
                self._hotkey = HotkeyMonitor(self)
            ok = self._hotkey.start(CFG["hotkey"], CFG["mode"])
            if not ok and self.state == "idle":
                self.status_item.title = (
                    "Status: enable Accessibility → quit & reopen Koovu")
            elif ok and self.status_item.title.startswith(
                    "Status: enable Accessibility"):
                self.status_item.title = "Status: idle"

        callAfter(_install)

    # ---- record / process

    def _live_mode(self):
        return (CFG.get("engine") == "deepgram_live"
                and CFG.get("deepgram_api_key"))

    def _local_mode(self):
        return CFG.get("engine") == "local_batch"

    def _on_mic_denied(self):
        self.status_item.title = "Status: mic denied — see dialog"
        try:
            from AppKit import NSAlert, NSAlertStyleWarning
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Microphone was denied")
            alert.setInformativeText_(
                "Dictation needs the microphone.\n\n"
                "Option A: System Settings → Privacy & Security → "
                "Microphone → turn Koovu ON.\n\n"
                "Option B: Click Try again below (resets permission) "
                "and tap Allow this time.")
            alert.setAlertStyle_(NSAlertStyleWarning)
            alert.addButtonWithTitle_("Try again")
            alert.addButtonWithTitle_("Open Settings")
            alert.addButtonWithTitle_("Cancel")
            choice = alert.runModal()
            if choice == 1000:
                _reset_mic_tcc()
                _show_mic_prompt()
            elif choice == 1001:
                open_privacy_pane("mic")
        except Exception as e:
            log(f"mic denied alert failed: {e}")

    def begin(self):
        log("begin()")
        if self._hotkey and not self._hotkey.ok:
            play(SOUND_ERROR)
            self.status_item.title = (
                "Status: enable Accessibility, then quit & reopen Koovu")
            return
        mic_st = _mic_auth_status()
        if mic_st != 3:
            log(f"mic blocked status={mic_st}")
            if mic_st in (2, 1):
                _reset_mic_tcc()
                self.status_item.title = (
                    "Status: mic reset — quit Koovu, reopen, tap Ctrl")
                play(SOUND_ERROR)
                return
            self.status_item.title = "Status: allow microphone…"
            from PyObjCTools.AppHelper import callAfter
            callAfter(lambda: _show_mic_prompt(on_granted=self._start_recording))
            return
        self._start_recording()

    def _start_recording(self):
        log("_start_recording()")
        if self.state == "recording":
            return
        needs_groq = not self._local_mode() or CFG.get("cleanup")
        if needs_groq and not CFG["groq_api_key"]:
            play(SOUND_ERROR)
            self.status_item.title = "Status: add API key in Settings"
            return
        if self._local_mode() and LOCAL_ASR:
            if LOCAL_ASR.state == "downloading":
                play(SOUND_ERROR)
                self.status_item.title = "Status: downloading local model…"
                return
            if LOCAL_ASR.state == "error":
                print(f"[koovu] local whisper unavailable: {LOCAL_ASR.error}")
        if self._live_mode():
            if not CFG.get("deepgram_api_key"):
                play(SOUND_ERROR)
                self.status_item.title = "Status: add Deepgram key in Settings"
                self.settings.show()
                return
            try:
                self.dg = DeepgramStreamer()
                self.dg.start()
            except Exception as e:
                print(f"[koovu] deepgram start failed: {e}, "
                      f"falling back to batch")
                self.dg = None
                self.rec.start()
        else:
            self.dg = None
            if not self.rec.start():
                play(SOUND_ERROR)
                log("rec.start() failed")
                self.status_item.title = "Status: mic failed — check input device"
                return
        self.state = "recording"
        self.status_item.title = "Status: recording…"
        log("recording started")
        play(SOUND_START)

    def end(self):
        log("end()")
        play(SOUND_STOP)
        if getattr(self, "dg", None):
            raw, typed = self.dg.stop()
            self.dg = None
            self.state = "processing"
            self.status_item.title = "Status: polishing…"
            threading.Thread(target=self.process_live, args=(raw, typed),
                             daemon=True).start()
            return
        wav = self.rec.stop()
        if not wav:
            self.state = "idle"
            self.status_item.title = "Status: no audio heard — check mic"
            return
        self.state = "processing"
        self.status_item.title = "Status: transcribing…"
        threading.Thread(target=self.process, args=(wav,),
                         daemon=True).start()

    def _finish_live(self, final, short_status=None):
        push_history(final)
        track_words(final)
        short = short_status or (final if len(final) < 60 else final[:57] + "…")
        self.status_item.title = f"Last: {short}"
        self.state = "idle"

    def process_live(self, raw, typed_chars):
        """Live mode: swap polished text in place (select + paste, no backspace)."""
        app_name = frontmost_app()
        print(f"[koovu] raw : {raw!r}  ({typed_chars} chars typed live)")
        if not raw:
            if typed_chars:
                backspace(typed_chars)
            self.state = "idle"
            self.status_item.title = "Status: heard nothing"
            return
        final = raw
        if CFG.get("cleanup"):
            try:
                final = cleanup(raw, app_name)
            except Exception as e:
                print(f"[koovu] cleanup failed, keeping live text: {e}")
                self._finish_live(raw)
                return
        print(f"[koovu] out : {final!r}  (app: {app_name})")
        if final.strip() != raw.strip():
            replace_typed_text(typed_chars, final)
        subprocess.run(["pbcopy"], input=final, text=True)
        self._finish_live(final)

    def process(self, wav):
        log(f"process({len(wav)} bytes)")
        app_name = frontmost_app()
        use_local = (self._local_mode() and LOCAL_ASR
                     and LOCAL_ASR.available()
                     and LOCAL_ASR.state != "error")
        try:
            if use_local:
                t0 = time.time()
                raw = LOCAL_ASR.transcribe(wav)
                print(f"[koovu] local asr: {time.time()-t0:.2f}s")
            else:
                raw = transcribe(wav)
        except Exception as e:
            print(f"[koovu] transcription failed ({'local' if use_local else 'groq'}): {e}")
            if use_local and CFG.get("groq_api_key"):
                try:
                    raw = transcribe(wav)
                    print("[koovu] fell back to groq")
                except Exception as e2:
                    print(f"[koovu] groq fallback failed: {e2}")
                    play(SOUND_ERROR)
                    self.state = "idle"
                    self.status_item.title = "Status: transcription failed"
                    return
            else:
                play(SOUND_ERROR)
                self.state = "idle"
                self.status_item.title = "Status: transcription failed"
                return
        print(f"[koovu] raw : {raw!r}")
        final = raw
        if raw and CFG["cleanup"]:
            try:
                final = cleanup(raw, app_name)
            except Exception as e:
                print(f"[koovu] cleanup failed, using raw: {e}")
                final = raw
        print(f"[koovu] out : {final!r}  (app: {app_name})")
        log(f"out: {final!r}")
        if final:
            subprocess.run(["pbcopy"], input=final, text=True)
            paste_text(final)
            push_history(final)
            track_words(final)
            short = final if len(final) < 60 else final[:57] + "…"
            self.status_item.title = f"Last: {short}"
        else:
            self.status_item.title = "Status: heard nothing"
        self.state = "idle"

    # ---- icon animation

    def _late_startup(self):
        """Hook menu-bar click + show onboarding. Runs on the main thread
        ~1s after the run loop is alive (driven by the icon timer, which is
        the only startup mechanism that reliably fires)."""
        try:
            self.settings.attach_status_item(self._nsapp.nsstatusitem)
            print("[koovu] menu bar click hooked")
        except Exception as e:
            print(f"[koovu] status bar hook failed: {e}")
        if not CFG.get("onboarded"):
            try:
                self.onboarding.show()
                print("[koovu] onboarding shown")
            except Exception as e:
                print(f"[koovu] onboarding failed: {e}")
        # Accessibility at startup; mic only via explicit user action.
        try:
            request_accessibility()
            self.start_listener()
            if _mic_auth_status() == 0:
                def _ask_mic():
                    from PyObjCTools.AppHelper import callAfter
                    callAfter(_show_mic_prompt)
                threading.Timer(2.0, _ask_mic).start()
        except Exception as e:
            print(f"[koovu] permission request: {e}")

    def tick_icon(self, _):
        self._ticks += 1
        if (not self._startup_done and self._ticks >= 8
                and getattr(self, "_nsapp", None)):
            self._startup_done = True
            self._late_startup()
        # Retry hotkey monitor after user grants Accessibility in Settings.
        if self._ticks % 40 == 0:
            try:
                import HIServices
                if (HIServices.AXIsProcessTrusted()
                        and (not self._hotkey or not self._hotkey.ok)):
                    self.start_listener()
            except Exception:
                pass
        if self.state == "recording":
            src = getattr(self, "dg", None) or self.rec
            lvl = src.level
            frame = min(4, int(lvl * 6))
            self.icon = os.path.join(ASSETS, f"rec{frame}.png")
            if not self.bubble.visible:
                self.bubble.show()
            self.bubble.tick(lvl)
            dg = getattr(self, "dg", None)
            if dg and time.time() - dg.started_at > int(CFG["max_seconds"]):
                self.toggle_on = False
                self.end()
        elif self.state == "processing":
            if self.bubble.visible:
                self.bubble.hide()
            f = "proc" if int(time.time() * 2) % 2 else "idle"
            self.icon = os.path.join(ASSETS, f"{f}.png")
        else:
            if self.bubble.visible:
                self.bubble.hide()
            self.icon = os.path.join(ASSETS, "idle.png")

    # ---- menu actions

    def on_dictate(self, _):
        """Menu fallback if hotkey fails — tap to start/stop."""
        if self.state == "recording":
            self.toggle_on = False
            self.end()
        else:
            self.toggle_on = True
            self.begin()

    def on_settings(self, _):
        self.settings.show()

    def on_copy_last(self, _):
        hist = load_history()
        if hist:
            subprocess.run(["pbcopy"], input=hist[0]["text"], text=True)

    def on_quit(self, _):
        try:
            if self._hotkey:
                self._hotkey.stop()
        except Exception:
            pass
        rumps.quit_application()


def main():
    global APP, LOCAL_ASR, CFG
    CFG = load_config()
    if getattr(sys, "frozen", False):
        if CFG.get("engine") != "groq_batch":
            CFG["engine"] = "groq_batch"
            save_config(CFG)
            log("engine forced to groq_batch")
    LOCAL_ASR = LocalWhisper()
    if CFG.get("engine") == "local_batch" and LOCAL_ASR.available():
        LOCAL_ASR.preload()
    start_settings_server()
    APP = KoovuApp()
    APP.run()


if __name__ == "__main__":
    main()
