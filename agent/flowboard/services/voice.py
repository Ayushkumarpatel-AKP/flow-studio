"""Voiceover for Studio.

Works with no setup: Windows' built-in voices and Microsoft Edge's free online
voices (English and Hindi among them). If the user has added an OpenAI key in
Engines, its more natural voices appear too. Whatever the choice, a failing
engine falls through to the next one, so a render never dies because a voice was
unavailable.

Everything here is synchronous (the renderer runs in a worker thread) and
results are cached on disk by content, so re-rendering or re-previewing a line
costs nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from flowboard.config import STORAGE_DIR
from flowboard.services import engines

logger = logging.getLogger(__name__)

CACHE_DIR = STORAGE_DIR / "voice-cache"

EDGE_VOICES = [
    {"id": "en-US-GuyNeural", "label": "Guy — US man", "gender": "male"},
    {"id": "en-US-ChristopherNeural", "label": "Christopher — US man", "gender": "male"},
    {"id": "en-US-JennyNeural", "label": "Jenny — US woman", "gender": "female"},
    {"id": "en-US-AriaNeural", "label": "Aria — US woman", "gender": "female"},
    {"id": "en-GB-RyanNeural", "label": "Ryan — UK man", "gender": "male"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia — UK woman", "gender": "female"},
    {"id": "en-IN-PrabhatNeural", "label": "Prabhat — Indian English man", "gender": "male"},
    {"id": "en-IN-NeerjaNeural", "label": "Neerja — Indian English woman", "gender": "female"},
    {"id": "hi-IN-MadhurNeural", "label": "Madhur — Hindi man", "gender": "male"},
    {"id": "hi-IN-SwaraNeural", "label": "Swara — Hindi woman", "gender": "female"},
]
OPENAI_VOICES = [
    {"id": "onyx", "label": "Onyx — deep man", "gender": "male"},
    {"id": "echo", "label": "Echo — man", "gender": "male"},
    {"id": "fable", "label": "Fable — storyteller", "gender": "male"},
    {"id": "nova", "label": "Nova — bright woman", "gender": "female"},
    {"id": "shimmer", "label": "Shimmer — soft woman", "gender": "female"},
    {"id": "alloy", "label": "Alloy — neutral", "gender": "female"},
]

# Fallback order when the chosen engine fails.
FALLBACK = ["edge", "sapi"]


class VoiceError(Exception):
    pass


def _edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except ImportError:
        return False


_sapi_cache: Optional[list[dict[str, str]]] = None


def sapi_voices() -> list[dict[str, str]]:
    """Voices installed in Windows (empty elsewhere)."""
    global _sapi_cache
    if _sapi_cache is not None:
        return _sapi_cache
    out: list[dict[str, str]] = []
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                 ".GetInstalledVoices() | % { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Gender }"],
                capture_output=True, text=True, timeout=20)
            for line in r.stdout.splitlines():
                name, _, gender = line.strip().partition("|")
                if name:
                    out.append({"id": name, "label": name.replace("Microsoft ", "").replace(" Desktop", ""),
                                "gender": gender.lower() or "female"})
        except (OSError, subprocess.SubprocessError):
            pass
    _sapi_cache = out
    return out


def engine_list() -> list[dict[str, Any]]:
    """Every voice engine, whether it can be used right now, and its voices."""
    sapi = sapi_voices()
    key = engines.get_key("openai")
    st = (engines._load().get("state") or {}).get("openai") or {}
    has_tts = bool(key and (st.get("models") or {}).get("tts"))
    eleven = (engines._load().get("state") or {}).get("elevenlabs") or {}
    has_eleven = bool(engines.get_key("elevenlabs") and (eleven.get("models") or {}).get("tts"))
    return [
        {"id": "elevenlabs", "label": "ElevenLabs", "free": False, "available": has_eleven,
         "note": "The most realistic voices, from your own ElevenLabs account (uses your credits)."
                 if has_eleven else "Paste an ElevenLabs key and your voices appear here.",
         "voices": eleven.get("voices") or []},
        {"id": "edge", "label": "Edge voices", "free": True, "available": _edge_available(),
         "note": "Free, natural, includes Hindi. Needs internet.", "voices": EDGE_VOICES},
        {"id": "sapi", "label": "Windows voices", "free": True, "available": bool(sapi),
         "note": "Free and works offline. Sounds more robotic.", "voices": sapi},
        {"id": "openai", "label": "OpenAI voices", "free": False, "available": has_tts,
         "note": "Most natural. Add an OpenAI key in Engines." if not has_tts else "Pay per use with your OpenAI key.",
         "voices": OPENAI_VOICES},
    ]


def default_voices(engine: str) -> tuple[str, str]:
    """(left speaker, right speaker) — a man and a woman where possible."""
    for e in engine_list():
        if e["id"] == engine and e["voices"]:
            vs = e["voices"]
            male = next((v["id"] for v in vs if v["gender"] == "male"), vs[0]["id"])
            female = next((v["id"] for v in vs if v["gender"] == "female"), vs[-1]["id"])
            return male, female
    return "", ""


def audio_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _cache_path(engine: str, voice: str, speed: float, text: str, ext: str) -> Path:
    h = hashlib.sha1(f"{engine}|{voice}|{speed:.2f}|{text}".encode("utf-8")).hexdigest()[:20]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{h}.{ext}"


def _edge(text: str, voice: str, speed: float, out: Path) -> None:
    import edge_tts
    rate = f"{int(round((speed - 1) * 100)):+d}%"
    asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out)))


def _sapi(text: str, voice: str, speed: float, out: Path) -> None:
    env = dict(os.environ, VOICE_TEXT=text, VOICE_NAME=voice, VOICE_OUT=str(out),
               VOICE_RATE=str(max(-10, min(10, int(round((speed - 1) * 10))))))
    script = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "try { $s.SelectVoice($env:VOICE_NAME) } catch {}; $s.Rate=[int]$env:VOICE_RATE; "
              "$s.SetOutputToWaveFile($env:VOICE_OUT); $s.Speak($env:VOICE_TEXT); $s.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script], env=env, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not out.exists():
        raise VoiceError(r.stderr.strip()[:200] or "Windows voice failed")


def _openai(text: str, voice: str, speed: float, out: Path) -> None:
    key = engines.get_key("openai")
    model = ((engines._load().get("state") or {}).get("openai") or {}).get("models", {}).get("tts")
    if not key or not model:
        raise VoiceError("OpenAI voice is not set up")
    r = httpx.post(f"{engines.PROVIDERS['openai']['base_url']}/audio/speech",
                   headers={"Authorization": f"Bearer {key}"}, timeout=60,
                   json={"model": model, "voice": voice, "input": text, "speed": max(0.5, min(2.0, speed)),
                         "response_format": "mp3"})
    if r.status_code != 200:
        raise VoiceError(f"OpenAI voice replied {r.status_code}")
    out.write_bytes(r.content)


def _elevenlabs(text: str, voice: str, speed: float, out: Path) -> None:
    key = engines.get_key("elevenlabs")
    model = ((engines._load().get("state") or {}).get("elevenlabs") or {}).get("models", {}).get("tts")
    if not key or not model or not voice:
        raise VoiceError("ElevenLabs is not set up")
    r = httpx.post(f"{engines.PROVIDERS['elevenlabs']['base_url']}/text-to-speech/{voice}",
                   params={"output_format": "mp3_44100_128"}, headers={"xi-api-key": key}, timeout=90,
                   json={"text": text, "model_id": model, "voice_settings": {"speed": max(0.7, min(1.2, speed))}})
    if r.status_code != 200:
        detail = ""
        try:
            d = r.json().get("detail")
            detail = (d.get("message") or d.get("status") or "") if isinstance(d, dict) else str(d or "")
        except (ValueError, AttributeError):
            pass
        raise VoiceError(f"ElevenLabs replied {r.status_code} {detail}".strip())
    out.write_bytes(r.content)


_RUN = {"edge": (_edge, "mp3"), "sapi": (_sapi, "wav"), "openai": (_openai, "mp3"),
        "elevenlabs": (_elevenlabs, "mp3")}


def synthesize(text: str, engine: str, voice: str, speed: float = 1.0) -> tuple[Path, str]:
    """Speak ``text``; returns (audio file, engine actually used).

    Tries the chosen engine first, then the free ones, so a missing key or a
    dropped connection never stops a render.
    """
    text = text.strip()
    if not text:
        raise VoiceError("nothing to say")
    available = {e["id"]: e for e in engine_list() if e["available"]}
    order = [engine] + [e for e in FALLBACK if e != engine]
    last = "no voice engine is available"
    for eng in order:
        if eng not in available:
            continue
        v = voice if eng == engine and voice else default_voices(eng)[0]
        fn, ext = _RUN[eng]
        path = _cache_path(eng, v, speed, text, ext)
        if path.exists() and path.stat().st_size > 0:
            return path, eng
        try:
            fn(text, v, speed, path)
            if path.exists() and path.stat().st_size > 0:
                return path, eng
        except Exception as exc:  # fall through to the next engine
            last = f"{eng}: {exc}"
            logger.info("voice engine %s failed: %s", eng, exc)
            path.unlink(missing_ok=True)
    raise VoiceError(last)
