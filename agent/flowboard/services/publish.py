"""Send a finished video to the places the user connected (Telegram, Discord, a folder, YouTube, Drive).

Only official APIs are used for posting. Google sign-in is the standard OAuth "desktop app" flow:
the user brings their own free Google Cloud client (id + secret), signs in once in the browser, and a
refresh token is kept in the local connectors file.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from flowboard.services import connectors

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 50 * 1024 * 1024
DISCORD_LIMIT = 25 * 1024 * 1024
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_PENDING: dict[str, dict[str, Any]] = {}   # oauth state -> {cid, redirect, made}


class PublishError(Exception):
    pass


# ── Google sign-in ──────────────────────────────────────────────────────────

def _client(data: dict[str, Any]) -> tuple[str, str]:
    for cid in ("youtube", "drive"):
        c = data["creds"].get(cid) or {}
        if c.get("client_id") and c.get("client_secret"):
            return c["client_id"], c["client_secret"]
    raise PublishError("Save your Google Client ID and secret first.")


def google_authorize_url(cid: str, redirect_uri: str) -> str:
    data = connectors._load()
    spec = connectors.CONNECTORS.get(cid)
    if not spec or spec.get("oauth") != "google":
        raise PublishError("This connector does not use Google sign-in.")
    client_id, secret = _client(data)
    if secret.strip().endswith(".apps.googleusercontent.com"):
        raise PublishError("The Client ID and Client secret boxes look swapped. Put the long ID ending in "
                           ".apps.googleusercontent.com in 'Client ID' and the GOCSPX- code in 'Client secret', then press Save & test.")
    if not client_id.strip().endswith(".apps.googleusercontent.com"):
        raise PublishError("That is not a Google Client ID. It is a long number followed by .apps.googleusercontent.com "
                           "(not an email address). Copy it from Google Cloud → APIs & Services → Credentials.")
    for k in [k for k, v in _PENDING.items() if time.time() - v["made"] > 900]:
        _PENDING.pop(k, None)
    state = secrets.token_urlsafe(16)
    _PENDING[state] = {"cid": cid, "redirect": redirect_uri, "made": time.time()}
    have = (data["oauth"].get("google") or {}).get("scopes") or []
    scopes = sorted(set(have) | {spec["scope"]})
    return _GOOGLE_AUTH + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent", "state": state,
    })


async def google_finish(state: str, code: str, client: Optional[httpx.AsyncClient] = None) -> str:
    pending = _PENDING.pop(state, None)
    if pending is None:
        raise PublishError("This sign-in link expired. Press Sign in with Google again.")
    data = connectors._load()
    client_id, secret = _client(data)
    own = client is None
    c = client or httpx.AsyncClient(timeout=30)
    try:
        r = await c.post(_GOOGLE_TOKEN, data={"code": code, "client_id": client_id, "client_secret": secret,
                                               "redirect_uri": pending["redirect"], "grant_type": "authorization_code"})
    finally:
        if own:
            await c.aclose()
    if r.status_code != 200:
        raise PublishError("Google did not accept the sign-in: " + _err(r))
    tok = r.json()
    if not tok.get("refresh_token"):
        raise PublishError("Google did not return a refresh token. Remove the app at myaccount.google.com/permissions and try again.")
    scopes = sorted(set(tok.get("scope", "").split()))
    data["oauth"]["google"] = {"refresh_token": tok["refresh_token"], "scopes": scopes}
    for cid in ("youtube", "drive"):
        if connectors.CONNECTORS[cid]["scope"] in scopes and data["creds"].get(cid):
            data["enabled"][cid] = True
            data["state"][cid] = {"ok": True, "detail": "Signed in with Google.", "tested_at": int(time.time())}
    connectors._save(data)
    return pending["cid"]


async def _access_token(c: httpx.AsyncClient) -> str:
    data = connectors._load()
    refresh = (data["oauth"].get("google") or {}).get("refresh_token")
    if not refresh:
        raise PublishError("Sign in with Google first.")
    client_id, secret = _client(data)
    r = await c.post(_GOOGLE_TOKEN, data={"client_id": client_id, "client_secret": secret,
                                           "refresh_token": refresh, "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise PublishError("Google sign-in expired. Press Sign in with Google again. " + _err(r))
    return r.json()["access_token"]


def _err(r: httpx.Response) -> str:
    try:
        j = r.json()
        e = j.get("error")
        msg = (e.get("message") if isinstance(e, dict) else None) or j.get("error_description") or e or j.get("description") or ""
    except (ValueError, AttributeError):
        msg = r.text[:120]
    return f"(HTTP {r.status_code} {str(msg)[:160]})".replace("  ", " ")


# ── targets ─────────────────────────────────────────────────────────────────

def _caption(title: str, description: str, tags: list[str], limit: int) -> str:
    text = title.strip()
    if description.strip():
        text += "\n\n" + description.strip()
    if tags:
        text += "\n\n" + " ".join("#" + re.sub(r"\W+", "", t) for t in tags if t.strip())
    return text[:limit]


async def _telegram(c: httpx.AsyncClient, video: Path, title: str, description: str, tags: list[str], **_: Any) -> dict[str, Any]:
    creds = connectors._load()["creds"]["telegram"]
    if video.stat().st_size > TELEGRAM_LIMIT:
        raise PublishError("Telegram only takes videos up to 50 MB.")
    with video.open("rb") as fh:
        r = await c.post(f"https://api.telegram.org/bot{creds['bot_token']}/sendVideo",
                         data={"chat_id": creds["chat_id"], "caption": _caption(title, description, tags, 1000),
                               "supports_streaming": "true"},
                         files={"video": (video.name, fh, "video/mp4")})
    if r.status_code != 200:
        text = r.text.lower()
        if "can't send messages to the bot" in text:
            raise PublishError("The chat ID is your bot's own ID. Open Connect, then Telegram, send /start to your bot from your own account and press 'Find my chat'.")
        if "chat not found" in text or "forbidden" in text:
            raise PublishError("Telegram cannot reach that chat. Send /start to your bot first (or add it as an admin of the channel), then press 'Find my chat' in Connect.")
        raise PublishError("Telegram refused the video " + _err(r))
    return {"detail": "Sent to Telegram."}


async def _discord(c: httpx.AsyncClient, video: Path, title: str, description: str, tags: list[str], **_: Any) -> dict[str, Any]:
    creds = connectors._load()["creds"]["discord"]
    if video.stat().st_size > DISCORD_LIMIT:
        raise PublishError("Discord only takes videos up to 25 MB on a normal server.")
    with video.open("rb") as fh:
        r = await c.post(creds["webhook_url"], data={"content": _caption(title, description, tags, 1900)},
                         files={"files[0]": (video.name, fh, "video/mp4")})
    if r.status_code not in (200, 204):
        raise PublishError("Discord refused the video " + _err(r))
    return {"detail": "Posted to Discord."}


async def _folder(c: httpx.AsyncClient, video: Path, title: str, description: str = "", tags: Optional[list[str]] = None, **_: Any) -> dict[str, Any]:
    folder = Path(connectors._load()["creds"]["folder"]["path"]).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\- ]+", "", title).strip()[:60] or "flow-studio-video"
    dest = folder / f"{safe}.mp4"
    n = 2
    while dest.exists():
        dest = folder / f"{safe} ({n}).mp4"
        n += 1
    await _to_thread(shutil.copyfile, video, dest)
    return {"detail": f"Saved to {dest}"}


async def _to_thread(fn, *a):
    import asyncio
    return await asyncio.to_thread(fn, *a)


async def _youtube(c: httpx.AsyncClient, video: Path, title: str, description: str, tags: list[str],
                   privacy: str = "private", vertical: bool = True, **_: Any) -> dict[str, Any]:
    token = await _access_token(c)
    desc = description.strip()
    if vertical and "#shorts" not in (desc + title).lower():
        desc = (desc + "\n\n#Shorts").strip()
    meta = {"snippet": {"title": title.strip()[:100] or "Flow Studio video", "description": desc[:4900],
                        "tags": [t for t in tags if t.strip()][:15], "categoryId": "24"},
            "status": {"privacyStatus": privacy if privacy in ("private", "unlisted", "public") else "private",
                       "selfDeclaredMadeForKids": False}}
    size = video.stat().st_size
    start = await c.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                         headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                                  "X-Upload-Content-Length": str(size), "Content-Type": "application/json; charset=UTF-8"},
                         content=json.dumps(meta).encode("utf-8"))
    if start.status_code != 200 or not start.headers.get("location"):
        raise PublishError("YouTube would not start the upload " + _err(start))
    put = await c.put(start.headers["location"], headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
                      content=video.read_bytes(), timeout=600)
    if put.status_code not in (200, 201):
        raise PublishError("YouTube upload failed " + _err(put))
    vid = put.json().get("id")
    return {"detail": f"Uploaded to YouTube as {meta['status']['privacyStatus']}.", "url": f"https://youtu.be/{vid}" if vid else None}


async def _drive(c: httpx.AsyncClient, video: Path, title: str, description: str = "", tags: Optional[list[str]] = None, **_: Any) -> dict[str, Any]:
    token = await _access_token(c)
    boundary = secrets.token_hex(12)
    meta = json.dumps({"name": (re.sub(r"[\\/:*?\"<>|]+", "", title).strip()[:80] or "flow-studio-video") + ".mp4"})
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
            f"--{boundary}\r\nContent-Type: video/mp4\r\n\r\n").encode() + video.read_bytes() + f"\r\n--{boundary}--".encode()
    r = await c.post("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
                     headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"},
                     content=body, timeout=600)
    if r.status_code not in (200, 201):
        raise PublishError("Drive refused the video " + _err(r))
    return {"detail": "Saved to Google Drive.", "url": r.json().get("webViewLink")}


_TARGETS = {"telegram": _telegram, "discord": _discord, "folder": _folder, "youtube": _youtube, "drive": _drive}


async def publish(targets: list[str], video: Path, *, title: str, description: str = "", tags: Optional[list[str]] = None,
                  privacy: str = "private", vertical: bool = True,
                  client: Optional[httpx.AsyncClient] = None) -> list[dict[str, Any]]:
    """Send ``video`` to every ready target. One failing target never stops the others."""
    if not video.is_file():
        raise PublishError("The video file is missing. Render it again.")
    data = connectors._load()
    own = client is None
    c = client or httpx.AsyncClient(timeout=120, follow_redirects=True)
    results: list[dict[str, Any]] = []
    try:
        for t in targets:
            label = connectors.CONNECTORS.get(t, {}).get("label", t)
            fn = _TARGETS.get(t)
            if fn is None or not connectors.is_ready(t, data):
                results.append({"target": t, "label": label, "ok": False, "detail": "Not connected yet."})
                continue
            try:
                out = await fn(c, video, title, description, tags or [], privacy=privacy, vertical=vertical)
                results.append({"target": t, "label": label, "ok": True, **out})
            except PublishError as exc:
                results.append({"target": t, "label": label, "ok": False, "detail": str(exc)})
            except httpx.HTTPError as exc:
                results.append({"target": t, "label": label, "ok": False,
                                "detail": f"Could not reach {label} ({exc.__class__.__name__}). Check your internet and try again."})
            except OSError as exc:
                results.append({"target": t, "label": label, "ok": False, "detail": f"File problem: {exc.__class__.__name__}"})
    finally:
        if own:
            await c.aclose()
    return results
