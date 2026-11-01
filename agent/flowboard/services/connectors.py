"""Connectors — everything Flow Studio can plug into besides the AI engines.

Three groups:
  * picture  — extra places to draw pictures (free ones included), used when Flow's limit is reached
  * publish  — places a finished video can be sent (Telegram, Discord, a folder, YouTube, Drive)
  * soon     — services that are listed so the user can see the road map (no setup yet)

Plus *custom* connectors: the user describes any picture service that has an HTTP API
(address, key, request body, where the picture is in the reply) and it works like a built-in one.

Credentials live in a local file and are never sent back to the browser (only masked hints).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

STORE_PATH = Path(os.getenv("FLOWBOARD_CONNECTORS", Path.home() / ".flowboard" / "connectors.json"))

GOOGLE_STEPS = [
    "Open console.cloud.google.com and create a project (free)",
    "APIs & Services → Library: turn on 'YouTube Data API v3' (and 'Google Drive API' for Drive)",
    "APIs & Services → Credentials → Create credentials → OAuth client ID → type 'Desktop app'",
    "Copy the Client ID and Client secret and paste them here, then press Sign in with Google",
    "Got 'redirect_uri_mismatch'? Your client is a Web app. Create a new one of type Desktop app, or add this under Authorised redirect URIs: http://127.0.0.1:8434/api/connectors/google/callback",
]

CONNECTORS: dict[str, dict[str, Any]] = {
    # ── pictures ────────────────────────────────────────────────────────────
    "cloudflare": {
        "group": "picture", "label": "Cloudflare", "icon": None, "color": "#f6821f", "emoji": "☁️",
        "tagline": "Free FLUX.2 pictures that keep your characters the same",
        "free": "Free: 10,000 neurons a day, about 7 pictures with a reference. Resets at 00:00 UTC.",
        "fields": [{"name": "account_id", "label": "Account ID", "secret": False, "placeholder": "32 letters and numbers"},
                   {"name": "token", "label": "API token", "secret": True, "placeholder": "cfat_… or a Workers AI token"}],
        "steps": ["Sign in at dash.cloudflare.com (free) and copy your Account ID from the Workers & Pages page",
                  "My Profile → API Tokens → Create Token → use the 'Workers AI' template",
                  "Copy the token and paste it here with your Account ID"],
        "get_key_url": "https://dash.cloudflare.com/profile/api-tokens",
    },
    "pollinations": {
        "group": "picture", "label": "Pollinations", "icon": None, "color": "#d9468f", "emoji": "🌸",
        "tagline": "Free pictures with no key — a last resort",
        "free": "Free, but the public service is often busy (it may answer 'at its limit'). Simpler pictures than Flow.",
        "fields": [], "steps": ["Nothing to paste — press Turn on."],
    },
    "huggingface": {
        "group": "picture", "label": "Hugging Face", "icon": "huggingface", "color": "#e0a100",
        "tagline": "Free FLUX pictures with a free token",
        "free": "Free tier with a daily limit.",
        "fields": [{"name": "token", "label": "Access token", "secret": True, "placeholder": "hf_…"}],
        "steps": ["Sign in at huggingface.co", "Settings → Access Tokens → Create new token (type: Read)",
                  "Copy the token that starts with hf_ and paste it here"],
        "get_key_url": "https://huggingface.co/settings/tokens",
    },
    "stability": {
        "group": "picture", "label": "Stability AI", "icon": "stability", "color": "#7c3aed",
        "tagline": "Stable Image — sharp, fast pictures",
        "free": "Small free credit when you sign up, then pay as you go.",
        "fields": [{"name": "key", "label": "API key", "secret": True, "placeholder": "sk-…"}],
        "steps": ["Sign in at platform.stability.ai", "Open API Keys → Create API Key", "Copy the key and paste it here"],
        "get_key_url": "https://platform.stability.ai/account/keys",
    },
    "fal": {
        "group": "picture", "label": "fal.ai", "icon": "fal", "color": "#7b3fe4",
        "tagline": "Very fast FLUX pictures",
        "free": "Small free credit when you sign up, then pay as you go.",
        "fields": [{"name": "key", "label": "API key", "secret": True, "placeholder": "id:secret"}],
        "steps": ["Sign in at fal.ai", "Dashboard → Keys → Add key", "Copy the key (looks like id:secret) and paste it here"],
        "get_key_url": "https://fal.ai/dashboard/keys",
    },
    "replicate": {
        "group": "picture", "label": "Replicate", "icon": "replicate", "color": "#111827",
        "tagline": "Thousands of models, FLUX by default",
        "free": "Pay as you go (a few cents per picture).",
        "fields": [{"name": "token", "label": "API token", "secret": True, "placeholder": "r8_…"}],
        "steps": ["Sign in at replicate.com", "Account → API tokens → Create token", "Copy the token that starts with r8_ and paste it here"],
        "get_key_url": "https://replicate.com/account/api-tokens",
    },
    # ── publish ─────────────────────────────────────────────────────────────
    "youtube": {
        "group": "publish", "label": "YouTube Shorts", "icon": "youtube", "color": "#ff0000", "oauth": "google",
        "scope": "https://www.googleapis.com/auth/youtube.upload",
        "tagline": "Upload the finished video as a Short",
        "free": "Free. About 6 uploads a day on the default quota. Uploads from a new app start as Private.",
        "fields": [{"name": "client_id", "label": "Client ID", "secret": False, "placeholder": "123…apps.googleusercontent.com"},
                   {"name": "client_secret", "label": "Client secret", "secret": True, "placeholder": "GOCSPX-…"}],
        "steps": GOOGLE_STEPS, "get_key_url": "https://console.cloud.google.com/apis/credentials",
    },
    "drive": {
        "group": "publish", "label": "Google Drive", "icon": "googledrive", "color": "#1a73e8", "oauth": "google",
        "scope": "https://www.googleapis.com/auth/drive.file",
        "tagline": "Save every video to your Drive",
        "free": "Free (uses your Drive space).",
        "fields": [{"name": "client_id", "label": "Client ID", "secret": False, "placeholder": "123…apps.googleusercontent.com"},
                   {"name": "client_secret", "label": "Client secret", "secret": True, "placeholder": "GOCSPX-…"}],
        "steps": GOOGLE_STEPS, "get_key_url": "https://console.cloud.google.com/apis/credentials",
    },
    "telegram": {
        "group": "publish", "label": "Telegram", "icon": "telegram", "color": "#229ed9",
        "tagline": "Send the video to a chat or channel",
        "free": "Free. Videos up to 50 MB.",
        "fields": [{"name": "bot_token", "label": "Bot token", "secret": True, "placeholder": "123456:ABC…"},
                   {"name": "chat_id", "label": "Your chat ID (or @channel)", "secret": False, "placeholder": "Use 'Find my chat', or a number / @channelname"}],
        "steps": ["In Telegram open @BotFather and send /newbot, then copy the bot token it gives you",
                  "Paste the token here and press Save (leave the chat ID empty for now)",
                  "Open your new bot in Telegram from YOUR account and send it /start",
                  "Press 'Find my chat' and your own chat ID is filled in for you. For a channel, add the bot as an admin and use @channelname instead"],
        "get_key_url": "https://t.me/BotFather",
    },
    "discord": {
        "group": "publish", "label": "Discord", "icon": "discord", "color": "#5865f2",
        "tagline": "Post the video in a channel",
        "free": "Free. Videos up to 25 MB.",
        "fields": [{"name": "webhook_url", "label": "Webhook URL", "secret": True, "placeholder": "https://discord.com/api/webhooks/…"}],
        "steps": ["Open Channel settings → Integrations → Webhooks → New Webhook", "Press Copy Webhook URL and paste it here"],
    },
    "folder": {
        "group": "publish", "label": "Folder on this computer", "icon": None, "color": "#3b82f6", "emoji": "📁",
        "tagline": "Copy every finished video into a folder",
        "free": "Free.",
        "fields": [{"name": "path", "label": "Folder path", "secret": False, "placeholder": r"C:\Users\you\Videos\Flow Studio"}],
        "steps": ["Paste the full path of a folder (it is created if it does not exist)"],
    },
    # ── road map (listed, no setup yet) ─────────────────────────────────────
    "instagram": {"group": "soon", "label": "Instagram Reels", "icon": "instagram", "color": "#e1306c",
                  "tagline": "Needs a Business/Creator account and a public video link", "free": "Official API is free."},
    "facebook": {"group": "soon", "label": "Facebook Reels", "icon": "facebook", "color": "#1877f2",
                 "tagline": "Same Meta app as Instagram", "free": "Official API is free."},
    "tiktok": {"group": "soon", "label": "TikTok", "icon": "tiktok", "color": "#111111",
               "tagline": "Needs TikTok app approval (drafts first)", "free": "Official API is free."},
    "runway": {"group": "soon", "label": "Runway", "icon": "runway", "color": "#111111",
               "tagline": "AI video clips", "free": "Pay as you go."},
    "luma": {"group": "soon", "label": "Luma", "icon": "luma", "color": "#6d28d9",
             "tagline": "AI video clips", "free": "Pay as you go."},
    "kling": {"group": "soon", "label": "Kling", "icon": "kling", "color": "#0ea5e9",
              "tagline": "AI video clips", "free": "Pay as you go."},
    "suno": {"group": "soon", "label": "Suno", "icon": "suno", "color": "#111111",
             "tagline": "Background music for the story", "free": "Free credits daily."},
}

PICTURE_IDS = [k for k, v in CONNECTORS.items() if v["group"] == "picture"]
PUBLISH_IDS = [k for k, v in CONNECTORS.items() if v["group"] == "publish"]


class ConnectorError(Exception):
    pass


# ── storage ─────────────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for k, v in (("creds", {}), ("enabled", {}), ("state", {}), ("oauth", {}), ("custom", []), ("order", [])):
        data.setdefault(k, v)
    return data


def _save(data: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(STORE_PATH)


def mask(value: str) -> str:
    v = value or ""
    if len(v) <= 10:
        return "•" * len(v)
    return f"{v[:4]}…{v[-4:]}"


def _spec(cid: str, data: dict[str, Any]) -> Optional[dict[str, Any]]:
    if cid in CONNECTORS:
        return CONNECTORS[cid]
    for c in data.get("custom", []):
        if c["id"] == cid:
            return {"group": "picture", "label": c["label"], "icon": None, "color": "#0f766e", "emoji": "🧩",
                    "tagline": "Your own picture service", "free": "Depends on the service.", "fields": [],
                    "steps": [], "custom": True}
    return None


def is_ready(cid: str, data: Optional[dict[str, Any]] = None) -> bool:
    """Connected and switched on (OAuth ones also need a completed sign-in)."""
    data = data or _load()
    spec = _spec(cid, data)
    if spec is None or spec["group"] == "soon":
        return False
    if not data["enabled"].get(cid, False):
        return False
    if spec.get("oauth"):
        tok = data["oauth"].get(spec["oauth"]) or {}
        return bool(tok.get("refresh_token")) and spec.get("scope") in (tok.get("scopes") or [])
    if spec.get("custom"):
        return True
    creds = data["creds"].get(cid, {})
    return all(creds.get(f["name"]) for f in spec["fields"])


def picture_order(data: Optional[dict[str, Any]] = None) -> list[str]:
    """Ready picture connectors, in the user's chosen order (custom ones included)."""
    data = data or _load()
    ids = PICTURE_IDS + [c["id"] for c in data["custom"]]
    chosen = [i for i in data["order"] if i in ids]
    ordered = chosen + [i for i in ids if i not in chosen]
    return [i for i in ordered if is_ready(i, data)]


def has_picture_connector() -> bool:
    return bool(picture_order())


def _view(cid: str, spec: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    creds = data["creds"].get(cid, {})
    google = data["oauth"].get(spec["oauth"], {}) if spec.get("oauth") else None
    signed = bool(google and google.get("refresh_token") and spec.get("scope") in (google.get("scopes") or []))
    state = data["state"].get(cid) or {}
    ready = is_ready(cid, data)
    if spec["group"] == "soon":
        status = "soon"
    elif ready:
        status = "ok" if state.get("ok", True) else "error"
    elif spec.get("oauth") and creds.get("client_id") and creds.get("client_secret") and not signed:
        good = creds["client_id"].strip().endswith(".apps.googleusercontent.com") and state.get("ok") is not False
        status = "needs_signin" if good else "error"
    elif creds or data["enabled"].get(cid):
        status = "error" if state.get("ok") is False else "off"
    else:
        status = "off"
    return {
        "id": cid, "group": spec["group"], "label": spec["label"], "icon": spec.get("icon"),
        "emoji": spec.get("emoji"), "color": spec.get("color"), "tagline": spec.get("tagline"),
        "free": spec.get("free"), "steps": spec.get("steps", []), "get_key_url": spec.get("get_key_url"),
        "oauth": bool(spec.get("oauth")), "custom": bool(spec.get("custom")),
        "fields": [{**f, "value": (mask(creds.get(f["name"], "")) if f.get("secret") else creds.get(f["name"], "")),
                    "set": bool(creds.get(f["name"]))} for f in spec.get("fields", [])],
        "status": status, "enabled": bool(data["enabled"].get(cid)), "detail": state.get("detail"),
        "tested_at": state.get("tested_at"),
    }


def overview() -> dict[str, Any]:
    data = _load()
    items = [_view(cid, spec, data) for cid, spec in CONNECTORS.items()]
    for c in data["custom"]:
        spec = _spec(c["id"], data)
        view = _view(c["id"], spec, data)
        view["custom_def"] = {k: v for k, v in c.items() if k != "headers"}
        view["custom_def"]["header_names"] = sorted((c.get("headers") or {}).keys())
        items.append(view)
    return {"connectors": items, "picture_order": picture_order(data)}


# ── saving and testing ──────────────────────────────────────────────────────

_TEST_HEADERS = {"User-Agent": "FlowStudio/1.0"}


async def _test(cid: str, creds: dict[str, str], c: httpx.AsyncClient) -> str:
    """Return a short human note on success; raise ConnectorError with a plain reason on failure."""
    if cid == "pollinations":
        return "Ready — no key needed."
    if cid == "cloudflare":
        r = await c.get(f"https://api.cloudflare.com/client/v4/accounts/{creds['account_id']}/tokens/verify",
                        headers={"Authorization": f"Bearer {creds['token']}"})
        if r.status_code in (400, 401, 403):
            raise ConnectorError("Cloudflare did not accept that Account ID and token.")
        r.raise_for_status()
        q = await cloudflare_quota(creds=creds, client=c)
        return f"Token works. About {q['left']} pictures left today."
    if cid == "huggingface":
        r = await c.get("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {creds['token']}"})
        if r.status_code == 401:
            raise ConnectorError("Hugging Face did not accept that token.")
        r.raise_for_status()
        return f"Signed in as {r.json().get('name', 'your account')}."
    if cid == "stability":
        r = await c.get("https://api.stability.ai/v1/user/account", headers={"Authorization": f"Bearer {creds['key']}"})
        if r.status_code in (401, 403):
            raise ConnectorError("Stability did not accept that key.")
        r.raise_for_status()
        credits = r.json().get("credits")
        return f"Key works. Credits left: {credits:.0f}." if isinstance(credits, (int, float)) else "Key works."
    if cid == "replicate":
        r = await c.get("https://api.replicate.com/v1/account", headers={"Authorization": f"Bearer {creds['token']}"})
        if r.status_code in (401, 403):
            raise ConnectorError("Replicate did not accept that token.")
        r.raise_for_status()
        return f"Signed in as {r.json().get('username', 'your account')}."
    if cid == "fal":
        if ":" not in creds["key"]:
            raise ConnectorError("A fal.ai key looks like id:secret (with a colon).")
        return "Saved. fal.ai cannot be tested without drawing a picture, so the first picture will confirm it."
    if cid == "telegram":
        base = f"https://api.telegram.org/bot{creds['bot_token']}"
        r = await c.get(f"{base}/getMe")
        if r.status_code != 200:
            raise ConnectorError("Telegram did not accept that bot token.")
        me = r.json().get("result", {})
        bot = me.get("username", "your bot")
        cid_text = creds["chat_id"].strip()
        if cid_text.lstrip("@").lower() == bot.lower() or cid_text == str(me.get("id", "")):
            raise ConnectorError(f"That is the bot's own ID (@{bot}), and a bot cannot message itself. "
                                 f"Open Telegram, send /start to @{bot} from YOUR account, then press 'Find my chat'.")
        if cid_text == "0":
            raise ConnectorError(f"Token works (bot @{bot}). Now open Telegram, send /start to @{bot} from your own account, and press 'Find my chat'.")
        r2 = await c.get(f"{base}/getChat", params={"chat_id": creds["chat_id"]})
        if r2.status_code != 200:
            raise ConnectorError(f"The bot @{bot} works, but it cannot see that chat. For yourself: send /start to @{bot} first, then press 'Find my chat'. For a channel: add the bot as an admin.")
        return f"Bot @{bot} can post to {r2.json().get('result', {}).get('title') or creds['chat_id']}."
    if cid == "discord":
        if not re.match(r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$", creds["webhook_url"]):
            raise ConnectorError("That does not look like a Discord webhook URL.")
        r = await c.get(creds["webhook_url"])
        if r.status_code != 200:
            raise ConnectorError("Discord did not accept that webhook (was it deleted?).")
        return f"Webhook works ({r.json().get('name', 'channel')})."
    if cid == "folder":
        p = Path(creds["path"]).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".flowstudio-write-test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            raise ConnectorError(f"Cannot write to that folder ({exc.__class__.__name__}).")
        return "Folder is ready."
    if cid in ("youtube", "drive"):
        cid_v, sec_v = creds["client_id"].strip(), creds["client_secret"].strip()
        id_ok = cid_v.endswith(".apps.googleusercontent.com")
        if sec_v.endswith(".apps.googleusercontent.com") and not id_ok:
            raise ConnectorError("The two boxes look swapped: the long ID ending in .apps.googleusercontent.com belongs in "
                                 "'Client ID', and the short code starting with GOCSPX- belongs in 'Client secret'.")
        if not id_ok:
            raise ConnectorError("The Client ID box must hold the long ID that ends in .apps.googleusercontent.com "
                                 "(not the name you gave the client). Copy it from Google Cloud → Clients.")
        return "Saved. Now press Sign in with Google."
    raise ConnectorError("Unknown connector.")


async def save(cid: str, values: dict[str, str], client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    data = _load()
    spec = _spec(cid, data)
    if spec is None or spec["group"] == "soon" or spec.get("custom"):
        raise ConnectorError("This connector cannot be set up.")
    creds = dict(data["creds"].get(cid, {}))
    for f in spec["fields"]:
        v = (values.get(f["name"]) or "").strip()
        if v:
            creds[f["name"]] = v
    if cid == "telegram" and creds.get("bot_token") and not creds.get("chat_id"):
        creds["chat_id"] = "0"                      # placeholder until 'Find my chat' fills it in
    missing = [f["label"] for f in spec["fields"] if not creds.get(f["name"])]
    if missing:
        raise ConnectorError("Please fill in: " + ", ".join(missing))
    own = client is None
    c = client or httpx.AsyncClient(timeout=20, headers=_TEST_HEADERS)
    try:
        note = await _test(cid, creds, c)
        ok = True
    except ConnectorError as exc:
        note, ok = str(exc), False
    except httpx.HTTPError as exc:
        note, ok = f"Could not reach the service ({exc.__class__.__name__}).", False
    finally:
        if own:
            await c.aclose()
    data["creds"][cid] = creds
    data["state"][cid] = {"ok": ok, "detail": note, "tested_at": int(time.time())}
    if ok:
        data["enabled"][cid] = True
        if spec.get("oauth") == "google":          # one Google app serves YouTube and Drive
            for other in ("youtube", "drive"):
                if other != cid and not data["creds"].get(other):
                    data["creds"][other] = {k: creds[k] for k in ("client_id", "client_secret") if k in creds}
    _save(data)
    return {"ok": ok, "detail": note}


async def retest(cid: str) -> dict[str, Any]:
    data = _load()
    spec = _spec(cid, data)
    if spec is None or spec["group"] == "soon":
        raise ConnectorError("Unknown connector.")
    if spec.get("custom"):
        return await test_custom(cid)
    creds = data["creds"].get(cid, {})
    if spec["fields"] and not creds:
        raise ConnectorError("Nothing saved yet.")
    return await save(cid, {}) if spec["fields"] else await _enable(cid)


async def _enable(cid: str) -> dict[str, Any]:
    data = _load()
    data["enabled"][cid] = True
    data["state"][cid] = {"ok": True, "detail": "Ready — no key needed.", "tested_at": int(time.time())}
    _save(data)
    return {"ok": True, "detail": data["state"][cid]["detail"]}


async def enable_keyless(cid: str) -> dict[str, Any]:
    if cid != "pollinations":
        raise ConnectorError("This connector needs a key.")
    return await _enable(cid)


def set_enabled(cid: str, on: bool) -> None:
    data = _load()
    if _spec(cid, data) is None:
        raise ConnectorError("Unknown connector.")
    data["enabled"][cid] = bool(on)
    _save(data)


def remove(cid: str) -> None:
    data = _load()
    spec = _spec(cid, data)
    if spec is None:
        return
    if spec.get("custom"):
        data["custom"] = [c for c in data["custom"] if c["id"] != cid]
    data["creds"].pop(cid, None)
    data["enabled"].pop(cid, None)
    data["state"].pop(cid, None)
    if spec.get("oauth"):
        others = [o for o, s in CONNECTORS.items() if s.get("oauth") == spec["oauth"] and o != cid and data["creds"].get(o)]
        if not others:
            data["oauth"].pop(spec["oauth"], None)
    _save(data)


def set_picture_order(order: list[str]) -> None:
    data = _load()
    ids = set(PICTURE_IDS) | {c["id"] for c in data["custom"]}
    data["order"] = [i for i in order if i in ids]
    _save(data)


# ── custom picture connectors ───────────────────────────────────────────────

def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return "custom-" + (s[:30] or "service")


def save_custom(definition: dict[str, Any]) -> str:
    """Add or replace a custom picture connector.

    definition: label, url, headers {name: value}, body (JSON text with {prompt} {width} {height} {aspect}),
    result_kind ("base64" | "url" | "binary"), result_path (dot path into the JSON reply).
    """
    label = (definition.get("label") or "").strip()
    url = (definition.get("url") or "").strip()
    if not label or not re.match(r"^https?://", url):
        raise ConnectorError("Give it a name and a web address that starts with http:// or https://")
    kind = definition.get("result_kind") or "url"
    if kind not in ("base64", "url", "binary"):
        raise ConnectorError("Result must be base64, url or binary.")
    if kind != "binary" and not (definition.get("result_path") or "").strip():
        raise ConnectorError("Say where the picture is in the reply, for example data.0.url")
    body = definition.get("body") or ""
    if body.strip():
        try:
            json.loads(body.replace("{prompt}", "x").replace("{width}", "1").replace("{height}", "1").replace("{aspect}", "x"))
        except ValueError:
            raise ConnectorError("The request body must be valid JSON (use {prompt}, {width}, {height}, {aspect}).")
    data = _load()
    cid = definition.get("id") or _slug(label)
    old = next((c for c in data["custom"] if c["id"] == cid), None)
    headers = {k.strip(): v for k, v in (definition.get("headers") or {}).items() if k.strip()}
    if old:                                # keep an existing secret when the form sends the mask back
        for k, v in list(headers.items()):
            if "•" in v or "…" in v:
                headers[k] = (old.get("headers") or {}).get(k, "")
    entry = {"id": cid, "label": label, "url": url, "headers": headers, "body": body,
             "method": "GET" if definition.get("method") == "GET" else "POST",
             "result_kind": kind, "result_path": (definition.get("result_path") or "").strip()}
    data["custom"] = [c for c in data["custom"] if c["id"] != cid] + [entry]
    data["enabled"][cid] = True
    data["state"][cid] = {"ok": None, "detail": "Saved. Press Test to draw a sample picture.", "tested_at": None}
    _save(data)
    return cid


def _get_path(obj: Any, path: str) -> Any:
    for part in [p for p in path.split(".") if p != ""]:
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


async def draw_custom(entry: dict[str, Any], prompt: str, fmt: str, c: httpx.AsyncClient) -> tuple[bytes, str]:
    import base64
    w, h = SIZES.get(fmt, SIZES["16:9"])
    subs = {"{prompt}": json.dumps(prompt)[1:-1], "{width}": str(w), "{height}": str(h), "{aspect}": fmt}
    body = entry.get("body") or ""
    for k, v in subs.items():
        body = body.replace(k, v)
    from urllib.parse import quote
    url = entry["url"].replace("{prompt}", quote(prompt[:900]))
    for k in ("{width}", "{height}", "{aspect}"):
        url = url.replace(k, subs[k])
    headers = dict(entry.get("headers") or {})
    if entry.get("method") == "GET":
        r = await c.get(url, headers=headers)
    else:
        headers.setdefault("Content-Type", "application/json")
        r = await c.post(url, headers=headers, content=body.encode("utf-8"))
    if r.status_code >= 400:
        raise ConnectorError(f"{entry['label']} answered HTTP {r.status_code}: {r.text[:120]}")
    kind = entry["result_kind"]
    if kind == "binary":
        return r.content, r.headers.get("content-type", "image/png").split(";")[0]
    try:
        value = _get_path(r.json(), entry["result_path"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise ConnectorError(f"Could not find the picture at '{entry['result_path']}' in the reply.")
    if kind == "base64":
        value = re.sub(r"^data:[^,]+,", "", str(value))
        return base64.b64decode(value), "image/png"
    got = await c.get(str(value))
    got.raise_for_status()
    return got.content, got.headers.get("content-type", "image/png").split(";")[0]


async def test_custom(cid: str) -> dict[str, Any]:
    data = _load()
    entry = next((c for c in data["custom"] if c["id"] == cid), None)
    if entry is None:
        raise ConnectorError("Unknown connector.")
    try:
        async with httpx.AsyncClient(timeout=90, headers=_TEST_HEADERS) as c:
            img, _ = await draw_custom(entry, "a small red apple on a table", "1:1", c)
        ok, note = bool(img), "Drew a sample picture."
    except (ConnectorError, httpx.HTTPError, ValueError) as exc:
        ok, note = False, f"Test failed: {exc}"
    data["state"][cid] = {"ok": ok, "detail": note, "tested_at": int(time.time())}
    _save(data)
    return {"ok": ok, "detail": note}


# ── drawing pictures ────────────────────────────────────────────────────────

SIZES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1024, 1024)}
_STABILITY_RATIO = {"16:9": "16:9", "9:16": "9:16", "1:1": "1:1"}
_FAL_SIZE = {"16:9": "landscape_16_9", "9:16": "portrait_16_9", "1:1": "square_hd"}


def _http_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        msg = j.get("error") or j.get("message") or j.get("detail") or ""
        if isinstance(msg, dict):
            msg = msg.get("message", "")
        if isinstance(j.get("errors"), list):
            msg = "; ".join(str(e) for e in j["errors"])
    except (ValueError, AttributeError):
        msg = r.text[:100]
    return f"HTTP {r.status_code} {str(msg)[:140]}".strip()


async def _draw(cid: str, creds: dict[str, str], prompt: str, fmt: str, c: httpx.AsyncClient,
                ref: Optional[bytes] = None) -> tuple[bytes, str]:
    w, h = SIZES.get(fmt, SIZES["16:9"])
    if cid == "cloudflare":
        return await _cloudflare_draw(creds, prompt, fmt, c, ref)
    if cid == "pollinations":
        from urllib.parse import quote
        r = await c.get(f"https://image.pollinations.ai/prompt/{quote(prompt[:900])}",
                        params={"width": w, "height": h, "nologo": "true", "model": "flux", "seed": int(time.time()) % 100000})
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            raise ConnectorError(_http_text(r))
        return r.content, r.headers["content-type"].split(";")[0]
    if cid == "huggingface":
        r = await c.post("https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
                         headers={"Authorization": f"Bearer {creds['token']}", "Accept": "image/png"},
                         json={"inputs": prompt, "parameters": {"width": (w // 16) * 16, "height": (h // 16) * 16}})
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            raise ConnectorError(_http_text(r))
        return r.content, r.headers["content-type"].split(";")[0]
    if cid == "stability":
        r = await c.post("https://api.stability.ai/v2beta/stable-image/generate/core",
                         headers={"Authorization": f"Bearer {creds['key']}", "Accept": "image/*"},
                         files={"prompt": (None, prompt), "aspect_ratio": (None, _STABILITY_RATIO.get(fmt, "16:9")),
                                "output_format": (None, "png")})
        if r.status_code != 200:
            raise ConnectorError(_http_text(r))
        return r.content, r.headers.get("content-type", "image/png").split(";")[0]
    if cid == "fal":
        r = await c.post("https://fal.run/fal-ai/flux/schnell", headers={"Authorization": f"Key {creds['key']}"},
                         json={"prompt": prompt, "image_size": _FAL_SIZE.get(fmt, "landscape_16_9"), "num_images": 1})
        if r.status_code != 200:
            raise ConnectorError(_http_text(r))
        try:
            url = r.json()["images"][0]["url"]
        except (ValueError, KeyError, IndexError):
            raise ConnectorError("fal.ai gave an unexpected reply.")
        got = await c.get(url)
        got.raise_for_status()
        return got.content, got.headers.get("content-type", "image/jpeg").split(";")[0]
    if cid == "replicate":
        r = await c.post("https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                         headers={"Authorization": f"Bearer {creds['token']}", "Prefer": "wait"},
                         json={"input": {"prompt": prompt, "aspect_ratio": fmt, "output_format": "png", "num_outputs": 1}})
        if r.status_code not in (200, 201):
            raise ConnectorError(_http_text(r))
        pred = r.json()
        for _ in range(40):
            if pred.get("status") in ("succeeded", "failed", "canceled"):
                break
            await _sleep(2)
            pred = (await c.get(pred["urls"]["get"], headers={"Authorization": f"Bearer {creds['token']}"})).json()
        if pred.get("status") != "succeeded" or not pred.get("output"):
            raise ConnectorError(f"Replicate did not finish ({pred.get('status')}). {str(pred.get('error') or '')[:100]}")
        out = pred["output"]
        got = await c.get(out[0] if isinstance(out, list) else out)
        got.raise_for_status()
        return got.content, got.headers.get("content-type", "image/png").split(";")[0]
    raise ConnectorError("Unknown picture connector.")


# ── Cloudflare Workers AI (FLUX.2 klein 9B, draws from a reference picture) ─────────────────

CF_MODELS: dict[str, dict[str, Any]] = {
    "9b": {"id": "@cf/black-forest-labs/flux-2-klein-9b", "label": "FLUX.2 klein 9B", "cost": 1389,
           "note": "Best likeness: keeps your characters the same. About 7 pictures a day."},
    "4b": {"id": "@cf/black-forest-labs/flux-2-klein-4b", "label": "FLUX.2 klein 4B", "cost": 107,
           "note": "Many more pictures a day, but it can change your characters."},
}
CF_DEFAULT_MODEL = "9b"
CF_FREE_NEURONS = 10_000            # Cloudflare's free allowance per day; it resets at 00:00 UTC
CF_DEFAULT_COST = 1389              # fallback when a model's cost is unknown (the real cost is learned per model)
_CF_SIZES = {"16:9": (1024, 576), "9:16": (576, 1024), "1:1": (768, 768)}


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _cf_creds() -> dict[str, str]:
    return _load()["creds"].get("cloudflare", {})


def cloudflare_model(data: Optional[dict[str, Any]] = None) -> str:
    """The model key the user picked ('9b' or '4b')."""
    key = ((data or _load()).get("settings") or {}).get("cloudflare_model")
    return key if key in CF_MODELS else CF_DEFAULT_MODEL


def set_cloudflare_model(key: str) -> None:
    if key not in CF_MODELS:
        raise ConnectorError("Unknown model. Choose one of: " + ", ".join(CF_MODELS))
    data = _load()
    data.setdefault("settings", {})["cloudflare_model"] = key
    _save(data)


def _cf_cost(u: dict[str, Any], key: str) -> float:
    """Neurons one picture costs on this model: what we last measured, else the known default."""
    return float((u.get("costs") or {}).get(key) or CF_MODELS[key]["cost"])


def _cf_state(data: dict[str, Any]) -> dict[str, Any]:
    """Today's usage record: ``floor`` = what Cloudflare's report showed, ``local`` = neurons our own
    calls used since then (the report lags by a few minutes, so both are needed)."""
    u = data.setdefault("usage", {}).setdefault("cloudflare", {})
    if u.get("day") != _utc_day():
        keep = u.get("costs")
        u.clear()
        u.update({"day": _utc_day(), "floor": 0.0, "local": 0.0})
        if keep:
            u["costs"] = keep
    if u.get("per_image"):                      # older records stored one cost: it was the 9B's
        u.setdefault("costs", {})["9b"] = u.pop("per_image")
    u.setdefault("floor", 0.0)
    u.setdefault("local", float(u.pop("neurons", 0.0)))
    return u


def _cf_record(neurons: float, key: str) -> None:
    """Add what one call used (read from Cloudflare's reply) to today's local count."""
    data = _load()
    u = _cf_state(data)
    u["local"] = float(u["local"]) + neurons
    u.setdefault("costs", {})[key] = neurons
    _save(data)


async def cloudflare_quota(creds: Optional[dict[str, str]] = None, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """How much of today's free allowance is left. Cloudflare's usage report can be a few minutes
    behind our own calls, so ``used`` is the larger of the report and (last report + our calls since)."""
    creds = creds or _cf_creds()
    remote: Optional[float] = None
    if creds.get("account_id") and creds.get("token"):
        own = client is None
        c = client or httpx.AsyncClient(timeout=12, headers=_TEST_HEADERS)
        try:
            r = await c.post("https://api.cloudflare.com/client/v4/graphql",
                             headers={"Authorization": f"Bearer {creds['token']}"},
                             json={"query": "query($a:String!,$d:Date!){viewer{accounts(filter:{accountTag:$a}){"
                                            "aiInferenceAdaptiveGroups(limit:100,filter:{date_geq:$d}){sum{totalNeurons}}}}}",
                                   "variables": {"a": creds["account_id"], "d": _utc_day()}})
            groups = r.json()["data"]["viewer"]["accounts"][0]["aiInferenceAdaptiveGroups"]
            remote = sum(float(g["sum"]["totalNeurons"]) for g in groups)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            remote = None                                 # analytics not allowed for this token: own count only
        finally:
            if own:
                await c.aclose()
    data = _load()
    u = _cf_state(data)
    if remote is not None and remote >= float(u["floor"]) + float(u["local"]):
        u["floor"], u["local"] = remote, 0.0              # the report has caught up with our calls
    used = float(u["floor"]) + float(u["local"])
    if remote is not None:
        used = max(used, remote)
    _save(data)
    key = cloudflare_model(data)
    cost = _cf_cost(u, key)
    return {"used": round(used), "free": CF_FREE_NEURONS, "cost": round(cost), "model": key,
            "left": int(max(0.0, CF_FREE_NEURONS - used) // cost), "resets": "00:00 UTC",
            "models": [{"key": k, "label": m["label"], "note": m["note"],
                        "per_day": int(CF_FREE_NEURONS // _cf_cost(u, k))} for k, m in CF_MODELS.items()]}


def _cf_reference(raw: bytes) -> bytes:
    """Cloudflare wants a small reference picture: scale it to at most 512 pixels."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((512, 512))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


async def _cloudflare_draw(creds: dict[str, str], prompt: str, fmt: str, c: httpx.AsyncClient,
                           ref: Optional[bytes]) -> tuple[bytes, str]:
    import base64
    w, h = _CF_SIZES.get(fmt, _CF_SIZES["16:9"])
    q = await cloudflare_quota(creds, c)
    if q["left"] < 1:
        raise ConnectorError("Cloudflare's free pictures for today are used up. They come back at 00:00 UTC.")
    key = q["model"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{creds['account_id']}/ai/run/{CF_MODELS[key]['id']}"
    fields = {"prompt": prompt[:2000], "width": str(w), "height": str(h), "steps": "4"}
    files = {"input_image_0": ("ref.jpg", _cf_reference(ref), "image/jpeg")} if ref else None
    last = "no answer"
    for attempt in range(2):          # Cloudflare's filter sometimes flags a harmless prompt; one retry usually passes
        r = await c.post(url, headers={"Authorization": f"Bearer {creds['token']}"}, data=fields, files=files, timeout=120)
        if r.status_code == 200:
            try:
                img = base64.b64decode(r.json()["result"]["image"])
            except (ValueError, KeyError, TypeError):
                raise ConnectorError("Cloudflare gave an unexpected reply.")
            try:
                _cf_record(float(r.headers.get("cf-ai-neurons", CF_MODELS[key]["cost"])), key)
            except ValueError:
                _cf_record(float(CF_MODELS[key]["cost"]), key)
            return img, "image/jpeg"
        last = _http_text(r)
        if r.status_code not in (400, 500, 502, 503):
            break
    if "flagged" in last.lower():
        raise ConnectorError("Cloudflare's filter blocked this picture. Reword the scene and retry.")
    raise ConnectorError(last)


async def _sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


async def draw_picture(prompt: str, fmt: str = "16:9", client: Optional[httpx.AsyncClient] = None,
                       ref: Optional[bytes] = None) -> tuple[bytes, str, str]:
    """Draw on the first ready picture connector. Returns (bytes, mime, connector label).

    Raises ConnectorError if none is ready or all fail (message lists each reason)."""
    data = _load()
    order = picture_order(data)
    if not order:
        raise ConnectorError("No picture connector is turned on.")
    own = client is None
    c = client or httpx.AsyncClient(timeout=150, headers=_TEST_HEADERS, follow_redirects=True)
    errors: list[str] = []
    try:
        for cid in order:
            spec = _spec(cid, data)
            try:
                if spec.get("custom"):
                    entry = next(x for x in data["custom"] if x["id"] == cid)
                    img, mime = await draw_custom(entry, prompt, fmt, c)
                else:
                    img, mime = await _draw(cid, data["creds"].get(cid, {}), prompt, fmt, c, ref)
                if img:
                    return img, mime, spec["label"]
            except (ConnectorError, httpx.HTTPError) as exc:
                errors.append(f"{spec['label']}: {exc}")
                logger.info("picture connector %s failed: %s", cid, exc)
        raise ConnectorError("; ".join(errors) or "no picture connector worked")
    finally:
        if own:
            await c.aclose()


async def telegram_find_chat(client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Look at who messaged the bot and use the newest person (or channel) as the chat to post to."""
    data = _load()
    creds = data["creds"].get("telegram") or {}
    if not creds.get("bot_token"):
        raise ConnectorError("Save the bot token first.")
    own = client is None
    c = client or httpx.AsyncClient(timeout=20, headers=_TEST_HEADERS)
    try:
        me = await c.get(f"https://api.telegram.org/bot{creds['bot_token']}/getMe")
        if me.status_code != 200:
            raise ConnectorError("Telegram did not accept that bot token.")
        bot = me.json()["result"].get("username", "your bot")
        r = await c.get(f"https://api.telegram.org/bot{creds['bot_token']}/getUpdates", params={"limit": 100})
        if r.status_code != 200:
            raise ConnectorError("Could not read the bot's messages. If another program also uses this bot, stop it and try again.")
        found: dict[int, dict[str, Any]] = {}
        for u in r.json().get("result", []):
            for key in ("message", "channel_post", "edited_message", "my_chat_member"):
                part = u.get(key) or {}
                chat = part.get("chat")
                if chat and not (part.get("from") or {}).get("is_bot", False):
                    found[chat["id"]] = chat
    finally:
        if own:
            await c.aclose()
    if not found:
        raise ConnectorError(f"No message found yet. Open Telegram, find @{bot}, press Start (or send /start), then press 'Find my chat' again.")
    chat = list(found.values())[-1]
    name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or str(chat["id"])
    data = _load()
    data["creds"]["telegram"]["chat_id"] = str(chat["id"])
    _save(data)
    result = await save("telegram", {}, client)
    result["detail"] = f"Found your chat: {name}. " + result["detail"]
    return result
