"""Koovu account: Supabase auth + end-to-end-encrypted settings sync.

Security model:
  - Auth: Supabase GoTrue (email + password, email confirmation, reset).
  - Settings (hotkey, dictionary, ...) sync as plain JSON, protected by
    Postgres Row Level Security (each user can only reach their own row).
  - API keys (Groq/Deepgram) are encrypted CLIENT-SIDE with a key derived
    from the user's password (PBKDF2-HMAC-SHA256, 200k iterations, per-user
    random salt). The server only ever stores ciphertext.
  - Consequence: resetting a forgotten password makes old synced API keys
    undecryptable. Settings survive; the user re-enters API keys once.
"""

import base64
import json
import os
import threading
import time

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

SUPABASE_URL = "https://kvvdscjpyvodmnuuqwul.supabase.co"
SUPABASE_KEY = "sb_publishable_7oxx8MZkg643zsF2Uj5BIw_9PodxF8i"
AUTH_URL = f"{SUPABASE_URL}/auth/v1"
REST_URL = f"{SUPABASE_URL}/rest/v1"

SESSION_PATH = os.path.join(os.path.expanduser("~"), ".koovu", "session.json")

# Config fields that sync across devices (API keys handled separately,
# encrypted; onboarded/sample_rate stay per-device).
SYNC_FIELDS = ["hotkey", "mode", "language", "engine", "local_model",
               "deepgram_language", "output_style", "custom_vocabulary",
               "corrections", "cleanup", "cleanup_level", "sounds",
               "max_seconds", "llm_model"]

_lock = threading.Lock()


# ----------------------------------------------------------- session store --

def _load_session():
    try:
        with open(SESSION_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _save_session(sess):
    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
    with open(SESSION_PATH, "w") as f:
        json.dump(sess, f, indent=2)
    os.chmod(SESSION_PATH, 0o600)


def _clear_session():
    try:
        os.remove(SESSION_PATH)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------- encryption --

def _derive_key(password, salt_b64):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=base64.b64decode(salt_b64), iterations=200_000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def _encrypt_keys(enc_key, groq, deepgram):
    blob = json.dumps({"groq": groq or "", "deepgram": deepgram or ""})
    return Fernet(enc_key).encrypt(blob.encode()).decode()


def _decrypt_keys(enc_key, blob):
    try:
        raw = Fernet(enc_key.encode() if isinstance(enc_key, str) else enc_key
                     ).decrypt(blob.encode())
        return json.loads(raw)
    except (InvalidToken, Exception):
        return None


# -------------------------------------------------------------- auth calls --

def _headers(access_token=None):
    h = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
    if access_token:
        h["Authorization"] = f"Bearer {access_token}"
    return h


def sign_up(email, password, profile=None):
    """profile: optional dict of user metadata (first_name, last_name, phone)."""
    body = {"email": email, "password": password}
    if profile:
        body["data"] = {k: v for k, v in profile.items() if v}
    r = requests.post(f"{AUTH_URL}/signup", headers=_headers(),
                      json=body, timeout=15)
    if r.status_code >= 400:
        msg = r.json().get("msg") or r.json().get("error_description") or r.text
        return {"ok": False, "error": msg}
    data = r.json()
    if not data.get("session") and not data.get("access_token"):
        return {"ok": True, "needs_confirmation": True}
    return _store_and_finish(data if "access_token" in data
                             else data["session"], email, password)


def sign_in(email, password):
    r = requests.post(f"{AUTH_URL}/token?grant_type=password",
                      headers=_headers(),
                      json={"email": email, "password": password}, timeout=15)
    if r.status_code >= 400:
        try:
            msg = (r.json().get("error_description")
                   or r.json().get("msg") or r.text)
        except Exception:
            msg = r.text
        return {"ok": False, "error": msg}
    return _store_and_finish(r.json(), email, password)


def _store_and_finish(tok, email, password):
    salt = _get_remote_salt(tok["access_token"])
    if not salt:
        salt = base64.b64encode(os.urandom(16)).decode()
    enc_key = _derive_key(password, salt)
    sess = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + tok.get("expires_in", 3600) - 60,
        "email": email,
        "user_id": tok.get("user", {}).get("id", ""),
        "enc_key": enc_key.decode(),
        "kdf_salt": salt,
    }
    _save_session(sess)
    return {"ok": True, "email": email}


def sign_out():
    sess = _load_session()
    if sess:
        try:
            requests.post(f"{AUTH_URL}/logout",
                          headers=_headers(sess["access_token"]), timeout=10)
        except Exception:
            pass
    _clear_session()
    return {"ok": True}


def recover(email):
    r = requests.post(f"{AUTH_URL}/recover", headers=_headers(),
                      json={"email": email}, timeout=15)
    if r.status_code >= 400:
        try:
            return {"ok": False, "error": r.json().get("msg", r.text)}
        except Exception:
            return {"ok": False, "error": r.text}
    return {"ok": True}


def set_password(access_token, refresh_token, new_password):
    """Called from the reset page. Updates the password, then re-registers
    the session with a fresh encryption salt."""
    r = requests.put(f"{AUTH_URL}/user", headers=_headers(access_token),
                     json={"password": new_password}, timeout=15)
    if r.status_code >= 400:
        try:
            return {"ok": False, "error": r.json().get("msg", r.text)}
        except Exception:
            return {"ok": False, "error": r.text}
    user = r.json()
    email = user.get("email", "")
    # fresh salt + key; old synced API keys are unrecoverable by design
    salt = base64.b64encode(os.urandom(16)).decode()
    enc_key = _derive_key(new_password, salt)
    sess = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + 3000,
        "email": email,
        "user_id": user.get("id", ""),
        "enc_key": enc_key.decode(),
        "kdf_salt": salt,
    }
    _save_session(sess)
    return {"ok": True, "email": email}


def _refresh(sess):
    r = requests.post(f"{AUTH_URL}/token?grant_type=refresh_token",
                      headers=_headers(),
                      json={"refresh_token": sess["refresh_token"]},
                      timeout=15)
    if r.status_code >= 400:
        _clear_session()
        return None
    tok = r.json()
    sess.update({
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + tok.get("expires_in", 3600) - 60,
    })
    _save_session(sess)
    return sess


def current_session():
    """Valid session or None. Auto-refreshes expired tokens."""
    with _lock:
        sess = _load_session()
        if not sess:
            return None
        if time.time() > sess.get("expires_at", 0):
            sess = _refresh(sess)
        return sess


def status():
    sess = current_session()
    if not sess:
        return {"signed_in": False}
    return {"signed_in": True, "email": sess.get("email", "")}


# -------------------------------------------------------------------- sync --

def _get_remote_salt(access_token):
    try:
        r = requests.get(f"{REST_URL}/user_settings?select=kdf_salt",
                         headers=_headers(access_token), timeout=15)
        rows = r.json() if r.status_code == 200 else []
        if rows and rows[0].get("kdf_salt"):
            return rows[0]["kdf_salt"]
    except Exception:
        pass
    return None


def push(cfg):
    """Upload current settings + encrypted keys. Returns dict."""
    sess = current_session()
    if not sess:
        return {"ok": False, "error": "not signed in"}
    settings = {k: cfg.get(k) for k in SYNC_FIELDS if k in cfg}
    keys_enc = _encrypt_keys(sess["enc_key"].encode(),
                             cfg.get("groq_api_key", ""),
                             cfg.get("deepgram_api_key", ""))
    row = {"user_id": sess["user_id"], "settings": settings,
           "keys_enc": keys_enc, "kdf_salt": sess["kdf_salt"]}
    r = requests.post(
        f"{REST_URL}/user_settings",
        headers={**_headers(sess["access_token"]),
                 "Prefer": "resolution=merge-duplicates"},
        json=row, timeout=15)
    if r.status_code >= 400:
        return {"ok": False, "error": r.text[:200]}
    return {"ok": True}


def push_async(cfg):
    threading.Thread(target=lambda: push(dict(cfg)), daemon=True).start()


def pull():
    """Fetch remote settings. Returns {ok, settings, keys} — keys is None
    if there's nothing synced or the blob can't be decrypted."""
    sess = current_session()
    if not sess:
        return {"ok": False, "error": "not signed in"}
    r = requests.get(f"{REST_URL}/user_settings?select=*",
                     headers=_headers(sess["access_token"]), timeout=15)
    if r.status_code >= 400:
        return {"ok": False, "error": r.text[:200]}
    rows = r.json()
    if not rows:
        return {"ok": True, "settings": None, "keys": None}
    row = rows[0]
    keys = None
    if row.get("keys_enc"):
        keys = _decrypt_keys(sess["enc_key"], row["keys_enc"])
    return {"ok": True, "settings": row.get("settings") or {}, "keys": keys}
