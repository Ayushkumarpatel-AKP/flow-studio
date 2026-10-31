"""Bring-your-own-key AI engines (Groq, NVIDIA, OpenAI, Gemini, OpenRouter).

Everything here is data-driven: a provider is a row in ``PROVIDERS`` (base URL,
key prefix, preferred models, what it can do, what its free tier looks like).
All of them speak the OpenAI-compatible HTTP API, so one small client covers
every provider and a new one is a few lines in the table.

What a pasted key unlocks is *measured*, not guessed: a one-token chat call
proves the key works and returns the provider's rate-limit headers, and the
model list tells us which jobs (writing, speech-to-text, images, voice) the key
can do. Keys live only in a local file and are never sent back to the browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

STORE_PATH = Path(os.getenv("FLOWBOARD_ENGINES", Path.home() / ".flowboard" / "engines.json"))

# Jobs the app can hand to an engine. `wired` = actually used by the app today.
JOBS: dict[str, dict[str, Any]] = {
    # `model` = which of the provider's picked models the job uses ("fast" = small and quick).
    "script":  {"label": "Script writing",   "kind": "text",  "model": "text", "wired": True},
    "rewrite": {"label": "✨ Rewrite helper", "kind": "text",  "model": "fast", "wired": True},
    "ideas":   {"label": "Story ideas",      "kind": "text",  "model": "fast", "wired": True},
    "autoprompt": {"label": "Canvas auto-prompt", "kind": "text", "model": "fast", "wired": True},
    "planner": {"label": "Canvas planner",   "kind": "text",  "model": "text", "wired": True},
    "chat":    {"label": "Canvas chat agent", "kind": "text",  "model": "text", "wired": True},
    "vision":  {"label": "Picture description", "kind": "vision", "model": "vision", "wired": True},
    "subtitles": {"label": "Exact subtitle timing", "kind": "stt", "model": "stt", "wired": True},
    "voice":   {"label": "Voiceover",        "kind": "tts",   "model": "tts", "wired": True},
    "image":   {"label": "Pictures (backup for Flow)", "kind": "image", "model": "image", "wired": True},
}
TEXT_JOBS = [j for j, v in JOBS.items() if v["kind"] == "text"]
# Old per-feature names used by the Canvas AI layer -> Engines job ids.
FEATURE_JOBS = {"auto_prompt": "autoprompt", "planner": "planner", "vision": "vision", "chat_agent": "chat"}

PROVIDERS: dict[str, dict[str, Any]] = {
    "groq": {
        "label": "Groq",
        "tagline": "Very fast writing and speech-to-text",
        "base_url": "https://api.groq.com/openai/v1",
        "key_prefix": "gsk_",
        "get_key_url": "https://console.groq.com/keys",
        "get_key_steps": ["Sign in at console.groq.com (no card needed)", "Open API Keys → Create API Key", "Copy the key that starts with gsk_ and paste it here"],
        "free_tier": "Free tier available — no card, but with request limits.",
        "free_always": False,
        "models": {
            "text": ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "llama3-70b-8192", "llama-3.1-8b-instant"],
            "fast": ["llama-3.1-8b-instant", "llama3-8b-8192"],
            "vision": ["meta-llama/llama-4-scout-17b-16e-instruct", "llama-3.2-90b-vision-preview", "llama-3.2-11b-vision-preview"],
            "stt": ["whisper-large-v3-turbo", "whisper-large-v3"],
        },
        "stt_regex": r"whisper", "image_regex": None, "tts_regex": None,  # Groq voices exist but the app does not use them yet
        "best_for": ["script", "rewrite", "ideas", "subtitles"],
    },
    "gemini": {
        "label": "Google Gemini",
        "tagline": "Generous free tier, good all-rounder",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_prefix": "AIza",
        "get_key_url": "https://aistudio.google.com/apikey",
        "get_key_steps": ["Open aistudio.google.com/apikey", "Click Create API key", "Copy the key that starts with AIza and paste it here"],
        "free_tier": "Free tier available in Google AI Studio (rate-limited).",
        "free_always": False,
        "models": {"text": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"], "fast": ["gemini-2.0-flash", "gemini-1.5-flash"],
                   "vision": ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-1.5-flash"]},
        "stt_regex": None, "image_regex": r"imagen", "tts_regex": None,
        "best_for": ["script", "ideas"],
    },
    "nvidia": {
        "label": "NVIDIA",
        "tagline": "Many open models, free trial credits",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "key_prefix": "nvapi-",
        "get_key_url": "https://build.nvidia.com/",
        "get_key_steps": ["Sign in at build.nvidia.com", "Open any model and click Get API Key", "Copy the key that starts with nvapi- and paste it here"],
        "free_tier": "Free trial credits when you sign up; usage is limited.",
        "free_always": False,
        "models": {
            "text": ["meta/llama-3.3-70b-instruct", "meta/llama-3.1-70b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct", "meta/llama-3.1-8b-instruct"],
            "fast": ["meta/llama-3.1-8b-instruct"],
            "vision": ["meta/llama-3.2-90b-vision-instruct", "meta/llama-3.2-11b-vision-instruct", "microsoft/phi-3.5-vision-instruct"],
        },
        "stt_regex": None, "image_regex": r"flux|stable-diffusion|sdxl", "tts_regex": None,
        "best_for": ["script", "rewrite"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "tagline": "One key, many models — some free",
        "base_url": "https://openrouter.ai/api/v1",
        "images_url": "https://openrouter.ai/api/v1/images/models",   # its picture-model catalogue
        "image_api": "openrouter",
        "key_prefix": "sk-or-",
        "get_key_url": "https://openrouter.ai/keys",
        "get_key_steps": ["Sign in at openrouter.ai", "Open Keys → Create Key", "Copy the key that starts with sk-or- and paste it here"],
        "free_tier": "Models whose name ends in :free cost nothing (limited per day).",
        "free_always": False,
        "models": {
            "text": ["meta-llama/llama-3.3-70b-instruct:free", "meta-llama/llama-3.3-70b-instruct", "google/gemini-2.0-flash-exp:free"],
            "fast": ["meta-llama/llama-3.1-8b-instruct:free", "meta-llama/llama-3.1-8b-instruct"],
            "vision": ["google/gemini-2.0-flash-exp:free", "meta-llama/llama-3.2-11b-vision-instruct:free"],
        },
        "stt_regex": None, "image_regex": None, "tts_regex": None,
        "best_for": ["ideas", "image"],
    },
    "openai": {
        "label": "OpenAI",
        "tagline": "Best voices and pictures, pay as you go",
        "base_url": "https://api.openai.com/v1",
        "image_api": "openai",
        "key_prefix": "sk-",
        "get_key_url": "https://platform.openai.com/api-keys",
        "get_key_steps": ["Sign in at platform.openai.com and add billing", "Open API keys → Create new secret key", "Copy the key that starts with sk- and paste it here"],
        "free_tier": "No free tier — you pay per use (add a small balance first).",
        "free_always": False,
        "models": {
            "text": ["gpt-4o-mini", "gpt-4.1-mini", "gpt-4o"], "fast": ["gpt-4o-mini"],
            "vision": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
            "stt": ["whisper-1", "gpt-4o-mini-transcribe"],
            "tts": ["gpt-4o-mini-tts", "tts-1"], "image": ["gpt-image-1", "dall-e-3"],
        },
        "stt_regex": r"whisper|transcribe", "image_regex": r"gpt-image|dall-e", "tts_regex": r"tts",
        "best_for": ["voice", "image"],
    },
}

PROVIDERS["elevenlabs"] = {
    "label": "ElevenLabs",
    "tagline": "The most realistic voices",
    "api": "elevenlabs",   # not OpenAI-compatible: own key header, voice list and speech call
    "base_url": "https://api.elevenlabs.io/v1",
    "key_prefix": "sk_",
    "get_key_url": "https://elevenlabs.io/app/settings/api-keys",
    "get_key_steps": [
        "Sign in at elevenlabs.io (a free account works)",
        "Open Settings → API Keys → Create key. If you restrict it, let it read voices and use text-to-speech",
        "Copy the key (it starts with sk_) and paste it here",
    ],
    "free_tier": "Free plan: about 10,000 credits a month — a few minutes of speech. Paid plans add more.",
    "free_always": False,
    "models": {"tts": ["eleven_multilingual_v2"]},
    "stt_regex": None, "image_regex": None, "tts_regex": None,
    "best_for": ["voice"],
}
ELEVEN_VOICES_URL = "https://api.elevenlabs.io/v2/voices"

# Order in which configured engines are tried for text jobs, best first.
TEXT_PREFERENCE = ["groq", "gemini", "nvidia", "openrouter", "openai"]
_ORDER_FOR_KIND = {
    "text": TEXT_PREFERENCE,
    "vision": ["gemini", "openai", "nvidia", "groq", "openrouter"],  # picture-reading quality first
    "tts": ["elevenlabs", "openai"],                                  # most natural voice first
    "image": ["openrouter", "openai"],                                # picture engines (Flow draws first)
}


class EngineError(Exception):
    pass


# ── key detection ───────────────────────────────────────────────────────────

def detect_provider(key: str) -> Optional[str]:
    """Guess the provider from the start of a pasted key (longest prefix wins)."""
    key = (key or "").strip()
    best: Optional[str] = None
    for pid, p in PROVIDERS.items():
        pre = p["key_prefix"]
        if key.startswith(pre) and (best is None or len(pre) > len(PROVIDERS[best]["key_prefix"])):
            best = pid
    return best


def mask(key: str) -> str:
    key = key or ""
    return f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "••••"


# ── model selection / compatibility ─────────────────────────────────────────

def _norm(ids: list[str]) -> list[str]:
    return [i.split("/", 1)[1] if i.startswith("models/") else i for i in ids]


def pick_model(ids: list[str], prefs: list[str]) -> Optional[str]:
    have = set(ids)
    for m in prefs:
        if m in have:
            return m
    return None


_IMAGE_PREFS = [r"nano-banana", r"gemini.*image", r"flux", r"seedream", r"gpt-image", r"qwen-image", r"."]
_CHEAP = re.compile(r"flash|turbo|klein|schnell|mini|lite|fast", re.I)


def pick_image_model(ids: list[str]) -> Optional[str]:
    """A sensible picture model from a live catalogue: known families first, and
    within a family the small/fast variant (they cost less per picture)."""
    for pat in _IMAGE_PREFS:
        hits = [i for i in ids if re.search(pat, i, re.I)]
        if hits:
            return ([h for h in hits if _CHEAP.search(h)] or hits)[0]
    return None


def analyse_models(pid: str, ids: list[str]) -> dict[str, Any]:
    """Which jobs this key's model list can do, and the model we would use."""
    p = PROVIDERS[pid]
    ids = _norm(ids)
    prefs = p["models"]
    text = pick_model(ids, prefs.get("text", []))
    if text is None and ids:
        # Fall back to any plausible chat model so a newer catalogue still works.
        chatty = [i for i in ids if re.search(r"llama|gemini|gpt|mistral|qwen|nemotron|deepseek", i, re.I)
                  and not re.search(r"embed|whisper|tts|guard|image|vision-only|rerank", i, re.I)]
        text = chatty[0] if chatty else None
    fast = pick_model(ids, prefs.get("fast", [])) or text
    stt = pick_model(ids, prefs.get("stt", []))
    if stt is None and p["stt_regex"]:
        stt = next((i for i in ids if re.search(p["stt_regex"], i, re.I)), None)
    image = pick_model(ids, prefs.get("image", []))
    if image is None and p["image_regex"]:
        image = next((i for i in ids if re.search(p["image_regex"], i, re.I)), None)
    tts = pick_model(ids, prefs.get("tts", []))
    if tts is None and p["tts_regex"]:
        tts = next((i for i in ids if re.search(p["tts_regex"], i, re.I)), None)
    # Vision needs a model that is known to take pictures; no guessing from the name.
    vision = pick_model(ids, prefs.get("vision", []))
    return {"text": text, "fast": fast, "stt": stt, "image": image, "tts": tts, "vision": vision}


def job_compat(models: dict[str, Optional[str]]) -> dict[str, dict[str, Any]]:
    """Per job: can this key do it, with which model, and does the app use it yet."""
    out: dict[str, dict[str, Any]] = {}
    for job, meta in JOBS.items():
        model = models.get(meta["model"]) or (models.get("text") if meta["kind"] == "text" else None)
        out[job] = {"ok": bool(model), "model": model, "wired": meta["wired"]}
    return out


def tier_guess(pid: str, limits: dict[str, Optional[int]]) -> dict[str, Any]:
    """Best-effort free/paid read-out. Honest about being an estimate."""
    p = PROVIDERS[pid]
    note = p["free_tier"]
    if pid == "openai":
        return {"label": "Paid", "free": False, "note": note, "estimate": False}
    if pid == "groq":
        tpm = limits.get("tokens")
        if tpm is not None:
            free = tpm <= 30000
            return {"label": "Looks like free tier" if free else "Looks like a paid/developer plan",
                    "free": free, "note": note, "estimate": True}
    return {"label": "Free tier / trial available", "free": True, "note": note, "estimate": True}


# ── storage ─────────────────────────────────────────────────────────────────

def _load() -> dict[str, Any]:
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"keys": {}, "state": {}, "assignments": {}}


def _save(data: dict[str, Any]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(STORE_PATH)


def get_key(pid: str) -> Optional[str]:
    return (_load().get("keys") or {}).get(pid) or None


# ── probing a key ───────────────────────────────────────────────────────────

def _int(v: Optional[str]) -> Optional[int]:
    try:
        return int(float(v)) if v is not None else None
    except ValueError:
        return None


def _limits(headers: httpx.Headers) -> dict[str, Optional[int]]:
    return {
        "requests": _int(headers.get("x-ratelimit-limit-requests")),
        "tokens": _int(headers.get("x-ratelimit-limit-tokens")),
        "remaining_requests": _int(headers.get("x-ratelimit-remaining-requests")),
        "remaining_tokens": _int(headers.get("x-ratelimit-remaining-tokens")),
    }


async def probe(pid: str, key: str, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Test ``key`` against provider ``pid``. Never raises; returns a status dict."""
    p = PROVIDERS[pid]
    base = p["base_url"]
    auth = {"Authorization": f"Bearer {key}"}
    own = client is None
    c = client or httpx.AsyncClient(timeout=25)
    try:
        if p.get("api") == "elevenlabs":
            return await _probe_elevenlabs(pid, key, c)
        ids: list[str] = []
        try:
            r = await c.get(f"{base}/models", headers=auth)
            if r.status_code == 200:
                body = r.json()
                rows = body.get("data") or body.get("models") or []
                ids = [m.get("id") or m.get("name") for m in rows if isinstance(m, dict)]
                ids = [i for i in ids if i]
        except (httpx.HTTPError, ValueError):
            pass
        models = analyse_models(pid, ids)

        # The model list is public on some providers, so validity is decided by a
        # one-token chat call, which also returns the rate-limit headers.
        candidates = [m for m in [models["fast"], models["text"], *p["models"]["text"][:2]] if m]
        seen: set[str] = set()
        last_status = 0
        last_msg = ""
        for model in candidates:
            if model in seen:
                continue
            seen.add(model)
            try:
                r = await c.post(f"{base}/chat/completions", headers=auth, json={
                    "model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
            except httpx.HTTPError as exc:
                return {"status": "network", "message": f"Could not reach {p['label']}: {exc.__class__.__name__}"}
            last_status = r.status_code
            if r.status_code in (200, 429):
                limits = _limits(r.headers)
                models["text"] = models["text"] or model
                image_ids: list[str] = []
                if p.get("images_url"):
                    try:
                        ir = await c.get(p["images_url"], headers=auth)
                        if ir.status_code == 200:
                            rows = ir.json()
                            rows = rows.get("data") if isinstance(rows, dict) else rows
                            image_ids = [m["id"] for m in rows or [] if isinstance(m, dict) and m.get("id")]
                            models["image"] = models["image"] or pick_image_model(image_ids)
                    except (httpx.HTTPError, ValueError, TypeError):
                        pass
                tier = tier_guess(pid, limits)
                image_blocked = None
                if pid == "openrouter":
                    try:  # does this account have purchased credits? pictures need them
                        kr = await c.get(f"{base}/key", headers=auth)
                        info = (kr.json().get("data") or {}) if kr.status_code == 200 else {}
                        if info.get("is_free_tier"):
                            image_blocked = "OpenRouter pictures need purchased credits, and this account is on the free tier."
                            models["image"] = None
                            tier = {"label": "Free tier — text only (pictures need purchased credits)", "free": True,
                                    "note": p["free_tier"], "estimate": False}
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                        pass
                return {
                    "status": "ok" if r.status_code == 200 else "rate_limited",
                    "message": "Key works." if r.status_code == 200 else "Key is valid but currently rate-limited.",
                    "models": models, "limits": limits, "tier": tier,
                    "model_count": len(ids), "model_ids": ids, "image_ids": image_ids,
                    "image_blocked": image_blocked, "checked_at": int(time.time()),
                }
            # Some providers (Gemini) answer a bad key with a 400 that mentions the key.
            bad_key = r.status_code in (401, 403) or (
                r.status_code == 400 and re.search(r"api[ _-]?key", r.text[:600], re.I))
            if bad_key:
                return {"status": "invalid", "message": f"{p['label']} rejected this key. Check it was copied fully."}
            try:
                last_msg = (r.json().get("error") or {}).get("message", "")[:160]
            except (ValueError, AttributeError):
                last_msg = ""
        return {"status": "error", "message": f"{p['label']} replied {last_status}. {last_msg}".strip()}
    finally:
        if own:
            await c.aclose()


async def _probe_elevenlabs(pid: str, key: str, c: httpx.AsyncClient) -> dict[str, Any]:
    """Test an ElevenLabs key by listing its voices (also gives us the voice menu)."""
    p = PROVIDERS[pid]
    headers = {"xi-api-key": key}
    try:
        r = await c.get(ELEVEN_VOICES_URL, params={"page_size": 100}, headers=headers)
    except httpx.HTTPError as exc:
        return {"status": "network", "message": f"Could not reach ElevenLabs: {exc.__class__.__name__}"}
    if r.status_code in (401, 403):
        return {"status": "invalid", "message": "ElevenLabs rejected this key. Check it was copied fully, and that "
                                                 "a restricted key is allowed to read voices and use text-to-speech."}
    if r.status_code not in (200, 429):
        return {"status": "error", "message": f"ElevenLabs replied {r.status_code}."}
    voices: list[dict[str, str]] = []
    if r.status_code == 200:
        try:
            for v in r.json().get("voices") or []:
                labels = v.get("labels") or {}
                gender = str(labels.get("gender") or "").lower()
                accent = str(labels.get("accent") or "")
                extra = ", ".join(x for x in (gender, accent) if x)
                voices.append({"id": v["voice_id"], "label": f"{v.get('name', 'Voice')}" + (f" — {extra}" if extra else ""),
                               "gender": gender or "female", "category": str(v.get("category") or "")})
        except (ValueError, KeyError, TypeError):
            voices = []
    # Premade voices work on every plan; library voices can need a paid plan, so list them last.
    voices.sort(key=lambda v: (v["category"] != "premade", v["label"]))
    tier: dict[str, Any] = {"label": "Free plan available", "free": True, "note": p["free_tier"], "estimate": True}
    try:  # credits left, if the key is allowed to read its subscription
        sub = await c.get(f"{p['base_url']}/user/subscription", headers=headers)
        if sub.status_code == 200:
            j = sub.json()
            limit, used = int(j.get("character_limit") or 0), int(j.get("character_count") or 0)
            name = str(j.get("tier") or "free")
            if limit:
                tier = {"label": f"{name.title()} plan — {max(0, limit - used):,} of {limit:,} credits left",
                        "free": name == "free", "note": p["free_tier"], "estimate": False}
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    models = {"text": None, "fast": None, "stt": None, "image": None, "vision": None, "tts": p["models"]["tts"][0]}
    ok = r.status_code == 200
    return {"status": "ok" if ok else "rate_limited",
            "message": f"Key works. {len(voices)} voices ready." if ok else "Key is valid but currently rate-limited.",
            "models": models, "limits": {}, "tier": tier, "model_count": len(voices),
            "model_ids": list(p["models"]["tts"]), "voices": voices, "checked_at": int(time.time())}


# ── public API used by routes ───────────────────────────────────────────────

def _provider_view(pid: str, data: dict[str, Any]) -> dict[str, Any]:
    p = PROVIDERS[pid]
    key = (data.get("keys") or {}).get(pid)
    st = (data.get("state") or {}).get(pid) or {}
    return {
        "id": pid, "label": p["label"], "tagline": p["tagline"], "key_prefix": p["key_prefix"],
        "get_key_url": p["get_key_url"], "get_key_steps": p["get_key_steps"], "free_tier": p["free_tier"],
        "best_for": p["best_for"],
        "configured": bool(key), "key_hint": mask(key) if key else None,
        "status": st.get("status") if key else None, "message": st.get("message") if key else None,
        "tier": st.get("tier") if key else None, "limits": st.get("limits") if key else None,
        "compat": job_compat(st["models"]) if key and st.get("models") else None,
    }


def recommend(data: dict[str, Any]) -> dict[str, Any]:
    """Per-job pick among the working engines, plus a plain-English tip."""
    working = [pid for pid in PROVIDERS
              if (data.get("keys") or {}).get(pid) and ((data.get("state") or {}).get(pid) or {}).get("status") in ("ok", "rate_limited")]
    picks: dict[str, Optional[str]] = {}
    for job, meta in JOBS.items():
        order = _ORDER_FOR_KIND.get(meta["kind"], ["groq", "openai", "gemini", "nvidia"])
        compat = {pid: job_compat(data["state"][pid]["models"])[job]["ok"] for pid in working if data["state"][pid].get("models")}
        picks[job] = next((pid for pid in order if compat.get(pid)), None)
    tips: list[str] = []
    if not working:
        tips.append("Start with a free Groq key — it is the fastest way to get script writing working, and it needs no card.")
    else:
        if picks.get("script"):
            tips.append(f"Use {PROVIDERS[picks['script']]['label']} for scripts and ✨ rewrites.")
        if "groq" not in working:
            tips.append("Add a free Groq key for the fastest writing and for exact karaoke-subtitle timing.")
        if "openai" not in working:
            tips.append("Voiceover already works free. For even more natural voices, paste an ElevenLabs or OpenAI key — it switches on by itself.")
        tips.append("Keep Google Flow for pictures and animation — it is covered by your Flow plan.")
    return {"picks": picks, "tips": tips}


_NOT_CHAT = re.compile(r"embed|whisper|tts|guard|rerank|moderation|transcribe|dall-e|image|imagen|audio|realtime|playai|orpheus", re.I)


def assigned(data: dict[str, Any], job: str) -> dict[str, Optional[str]]:
    """The user's choice for a job as {engine, model, backup}. Old files stored a bare
    engine id; that still reads correctly."""
    raw = (data.get("assignments") or {}).get(job)
    if isinstance(raw, str):
        return {"engine": raw, "model": None, "backup": None}
    raw = raw or {}
    return {"engine": raw.get("engine"), "model": raw.get("model"), "backup": raw.get("backup")}


def assigned_engine(data: dict[str, Any], job: str) -> Optional[str]:
    return assigned(data, job)["engine"]


def available_models(pid: str, job: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Models of ``pid`` that make sense for ``job``, recommended one first.

    The list comes from what the user's own key can see, so it stays current as
    providers add models. Pictures are only offered for models known to read them.
    """
    st = (data.get("state") or {}).get(pid) or {}
    kind = JOBS[job]["kind"]
    ids = _norm(st.get("model_ids") or [])
    if kind == "text":
        cand = [i for i in ids if not _NOT_CHAT.search(i)]
    elif kind == "vision":
        known = set(PROVIDERS[pid]["models"].get("vision", []))
        cand = [i for i in ids if i in known]
    elif kind == "stt":
        cand = [i for i in ids if re.search(r"whisper|transcribe", i, re.I)]
    elif kind == "tts":
        cand = [i for i in ids if re.search(r"tts|eleven", i, re.I)]
    else:
        rx = PROVIDERS[pid].get("image_regex")
        cand = _norm(st.get("image_ids") or []) or [i for i in ids if rx and re.search(rx, i, re.I)]
    default = (st.get("models") or {}).get(JOBS[job]["model"])
    if default and default not in cand:
        cand.insert(0, default)
    return [{"id": i, "recommended": i == default} for i in sorted(set(cand), key=lambda x: (x != default, x))]


def _model_for(job: str, pid: str, data: dict[str, Any]) -> Optional[str]:
    """The model to use for ``job`` on ``pid``: the user's pick if it is theirs and
    still valid, otherwise the recommended one."""
    st = (data.get("state") or {}).get(pid) or {}
    models = st.get("models") or {}
    a = assigned(data, job)
    if a["engine"] == pid and a["model"]:
        return a["model"]
    slot = JOBS[job]["model"] if job in JOBS else "text"
    return models.get(slot) or (models.get("text") if slot in ("fast", "text") else None)


def job_status(data: dict[str, Any], job: str) -> dict[str, Any]:
    """What will actually run this job right now, in words a person can act on."""
    order = _text_order(job, data)
    a = assigned(data, job)
    if not order:
        blocked = next((st.get("image_blocked") for st in (data.get("state") or {}).values() if st.get("image_blocked")), None)
        tip = {"vision": "Add a free Gemini key to describe pictures.",
               "voice": "Free voices work without a key.",
               "image": (blocked + " " if blocked else "") + "Pictures come from your Flow plan; add an OpenAI key, or an "
                        "OpenRouter key with credits, as a backup."}.get(job, "Add a free Groq key to power this.")
        return {"state": "none", "engine": None, "message": f"No connected engine can do this. {tip}"}
    first = order[0]
    if job == "image":
        return {"state": "ok", "engine": first,
                "message": f"Flow draws first. When its daily limit is reached, {PROVIDERS[first]['label']} takes over "
                           "(this uses your own credits)."}
    st = ((data.get("state") or {}).get(first) or {}).get("status")
    note = ""
    if a["engine"] and a["engine"] != first:
        note = f" ({PROVIDERS[a['engine']]['label']} cannot do this, so {PROVIDERS[first]['label']} is used.)"
    backup = [p for p in order[1:2]]
    tail = f" Backup: {PROVIDERS[backup[0]]['label']}." if backup else ""
    if st == "rate_limited":
        return {"state": "limited", "engine": first, "message": f"{PROVIDERS[first]['label']} is rate-limited right now.{tail}{note}"}
    return {"state": "ok", "engine": first, "message": f"Using {PROVIDERS[first]['label']}.{tail}{note}"}


def overview() -> dict[str, Any]:
    data = _load()
    rec = recommend(data)
    jobs_cfg: dict[str, Any] = {}
    for job in JOBS:
        usable = _usable_engines(job, data)
        jobs_cfg[job] = {
            **assigned(data, job),
            "status": job_status(data, job),
            "options": [{"id": pid, "label": PROVIDERS[pid]["label"], "models": available_models(pid, job, data)}
                        for pid in usable],
        }
    return {
        "providers": [_provider_view(pid, data) for pid in PROVIDERS],
        "jobs": [{"id": j, **{k: v for k, v in m.items()}} for j, m in JOBS.items()],
        # kept for older callers: the engine each job is set to (or recommended for)
        "assignments": {j: assigned_engine(data, j) or rec["picks"].get(j) for j in JOBS},
        "config": jobs_cfg,
        "recommended": rec["picks"], "tips": rec["tips"],
    }


async def save_key(pid: str, key: str) -> dict[str, Any]:
    key = key.strip()
    result = await probe(pid, key)
    data = _load()
    # Keep keys that work, or that are merely rate-limited; refuse ones the provider rejects.
    if result["status"] in ("ok", "rate_limited"):
        data.setdefault("keys", {})[pid] = key
        data.setdefault("state", {})[pid] = {k: result[k] for k in ("status", "message", "models", "limits", "tier", "checked_at", "model_ids", "voices", "image_ids", "image_blocked") if k in result}
        _save(data)
    return result


async def retest(pid: str) -> dict[str, Any]:
    key = get_key(pid)
    if not key:
        raise EngineError("No key saved for this engine.")
    result = await probe(pid, key)
    data = _load()
    data.setdefault("state", {})[pid] = {k: result[k] for k in ("status", "message", "models", "limits", "tier", "checked_at", "model_ids", "voices", "image_ids", "image_blocked") if k in result}
    if "status" in result and result["status"] == "invalid":
        data["state"][pid]["status"] = "invalid"
    _save(data)
    return result


def remove_key(pid: str) -> None:
    data = _load()
    (data.get("keys") or {}).pop(pid, None)
    (data.get("state") or {}).pop(pid, None)
    for job in list((data.get("assignments") or {}).keys()):
        a = assigned(data, job)
        if a["engine"] == pid:
            data["assignments"].pop(job, None)
        elif a["backup"] == pid:
            a["backup"] = None
            data["assignments"][job] = a
    _save(data)


def set_assignment(job: str, engine: Optional[str] = None, model: Optional[str] = None,
                   backup: Optional[str] = None) -> None:
    """Save the user's choice for a job. Empty everything = back to Auto."""
    if job not in JOBS:
        raise EngineError("Unknown job.")
    data = _load()
    keys = data.get("keys") or {}
    for label, pid in (("engine", engine), ("backup", backup)):
        if pid and (pid not in PROVIDERS or not keys.get(pid)):
            raise EngineError(f"The {label} has no saved key.")
    if backup and backup == engine:
        raise EngineError("The backup must be a different engine.")
    if model:
        if not engine:
            raise EngineError("Pick an engine before picking its model.")
        if model not in {m["id"] for m in available_models(engine, job, data)}:
            raise EngineError("That model is not available for this job on your key.")
    if engine or backup:
        data.setdefault("assignments", {})[job] = {"engine": engine, "model": model, "backup": backup}
    else:
        (data.get("assignments") or {}).pop(job, None)
    _save(data)


# ── running text jobs ───────────────────────────────────────────────────────

def covers_canvas() -> bool:
    """True when connected engines can run the Canvas's text jobs (auto-prompt and
    planner). Picture description is optional — it fails gracefully without it — so
    it does not hold back the "set up your AI" gate."""
    data = _load()
    return all(_text_order(j, data) for j in ("autoprompt", "planner"))


def _usable_engines(job: str, data: dict[str, Any]) -> list[str]:
    """Connected engines whose key can do ``job``, best first by default."""
    slot = JOBS[job]["model"] if job in JOBS else "text"
    base = _ORDER_FOR_KIND.get(JOBS[job]["kind"] if job in JOBS else "text", ["groq", "openai", "gemini", "nvidia"])

    def usable(pid: str) -> bool:
        st = (data.get("state") or {}).get(pid) or {}
        models = st.get("models") or {}
        return bool((data.get("keys") or {}).get(pid)) and st.get("status") in ("ok", "rate_limited") \
            and bool(models.get(slot) or (slot in ("fast", "text") and models.get("text")))

    return [pid for pid in base if usable(pid)]


def _text_order(job: str, data: dict[str, Any]) -> list[str]:
    """Order to try engines for ``job``: the user's pick, its backup, then the rest."""
    usable = _usable_engines(job, data)
    a = assigned(data, job)
    first = [p for p in (a["engine"], a["backup"]) if p in usable]
    return first + [p for p in usable if p not in first]


def _image_parts(paths: Optional[list[str]]) -> list[dict[str, Any]]:
    """Attach local pictures to a chat message as data URLs (OpenAI-compatible)."""
    import base64
    import mimetypes
    parts: list[dict[str, Any]] = []
    for path in paths or []:
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts


_RETRYABLE = (429, 500, 502, 503, 504)
_RETRY_WAIT = 2.0   # seconds; tests set this to 0


def _retry_wait(r: httpx.Response) -> float:
    """How long to wait before the retry: the provider's own hint if it gave one."""
    try:
        hint = float(r.headers.get("retry-after", ""))
    except ValueError:
        hint = _RETRY_WAIT
    return max(0.0, min(hint, 8.0)) if _RETRY_WAIT else 0.0


async def chat(job: str, user: str, *, system_prompt: Optional[str] = None, timeout: float = 90.0,
               images: Optional[list[str]] = None, client: Optional[httpx.AsyncClient] = None) -> str:
    """Run a text job on the first working engine, falling through on failure.

    Raises ``EngineError`` when no engine is configured or all of them fail, so
    the caller can fall back to the built-in Claude CLI.
    """
    data = _load()
    order = _text_order(job, data)
    if not order:
        raise EngineError("no engine configured")
    own = client is None
    c = client or httpx.AsyncClient(timeout=timeout)
    errors: list[str] = []
    try:
        for pid in order:
            p = PROVIDERS[pid]
            models = (data["state"][pid].get("models") or {})
            model = _model_for(job, pid, data)
            if not model:
                continue
            content: Any = user
            if images:
                content = [{"type": "text", "text": user}, *_image_parts(images)]
            msgs = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": content}]
            try:
                r = await c.post(f"{p['base_url']}/chat/completions",
                                 headers={"Authorization": f"Bearer {data['keys'][pid]}"},
                                 json={"model": model, "messages": msgs, "temperature": 0.8})
                if r.status_code in _RETRYABLE:
                    # Free tiers rate-limit in bursts. One short wait usually clears it, and it is
                    # better than failing a long chat run halfway through.
                    wait = _retry_wait(r)
                    logger.info("%s answered %s; retrying once in %.1fs", pid, r.status_code, wait)
                    await asyncio.sleep(wait)
                    r = await c.post(f"{p['base_url']}/chat/completions",
                                     headers={"Authorization": f"Bearer {data['keys'][pid]}"},
                                     json={"model": model, "messages": msgs, "temperature": 0.8})
                if r.status_code == 200:
                    text = (r.json()["choices"][0]["message"]["content"] or "").strip()
                    if text:
                        return text
                    errors.append(f"{pid}: empty reply")
                else:
                    errors.append(f"{pid}: HTTP {r.status_code}")
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                errors.append(f"{pid}: {exc.__class__.__name__}")
        raise EngineError("; ".join(errors) or "no usable engine")
    finally:
        if own:
            await c.aclose()
