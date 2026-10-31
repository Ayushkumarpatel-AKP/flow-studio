"""Studio routes — the beginner-friendly "idea → comic video" flow.

The browser drives generation through the existing `/api/requests` endpoints
(images, then optional image-to-video). This module adds the two pieces that
had no home:

  * `POST /api/studio/script`  — turn a one-line idea into panels + dialogue
  * `POST /api/studio/render`  — draw bubbles/captions and join into one MP4
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from flowboard.config import STORAGE_DIR
from flowboard.services import engines, media as media_service
from flowboard.services import studio_render, subtitles, voice
from flowboard.services.llm import run_llm
from flowboard.services.llm.base import LLMError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/studio", tags=["studio"])


async def _llm(job: str, user: str, system: str, timeout: float) -> str:
    """Run a text job on the user's own engine (Groq, Gemini, …) when one is
    configured, otherwise on the built-in Claude provider."""
    try:
        return await engines.chat(job, user, system_prompt=system, timeout=timeout)
    except engines.EngineError as exc:
        logger.info("studio %s: own engine unavailable (%s) — using built-in AI", job, exc)
    return await run_llm("auto_prompt", user, system_prompt=system, timeout=timeout)

STUDIO_DIR = STORAGE_DIR / "studio"
_JOBS: dict[str, dict[str, Any]] = {}

STYLE_PRESETS = {
    "comic": "Western comic book panel, bold black ink outlines, halftone dot shading, flat vivid colors",
    "stickman": "Western comic book panel, bold black ink outlines, halftone shading, simple stickman "
                "characters with round white heads, thin black stick limbs and dot eyes",
    "anime": "anime key visual, clean cel shading, expressive characters, vibrant colors",
    "watercolor": "soft watercolor illustration, painterly textures, gentle colors",
}
# Studio overlays text itself, so the image model must not draw any, and must
# leave the top band empty for the speech bubbles.
NO_TEXT_SUFFIX = (
    " NO text, NO letters, NO speech bubbles, NO words anywhere. "
    "Leave the top 28 percent of the image as plain empty background for captions."
)

_SCRIPT_SYSTEM = (
    "You write short comic-book scripts. Reply with ONLY valid JSON, no markdown, in this shape:\n"
    '{"title": str, "characters": str, "panels": [{"scene": str, "caption": str, '
    '"bubbles": [{"side": "left"|"right", "text": str}], "sfx": str}]}\n'
    "Rules: 'characters' is one sentence describing each character's fixed look (reused in every "
    "panel for consistency). 'scene' describes only the visuals of that panel (no text in the "
    "picture). Each bubble text is at most 12 words. At most 2 bubbles per panel; 'left' speaks "
    "first. 'caption' is narration or empty. 'sfx' is a short sound word like BONK! or empty. "
    "Tell a clear story with a hook, rising tension and an ending beat."
)


class ScriptBody(BaseModel):
    idea: str = Field(min_length=3, max_length=500)
    panels: int = Field(default=8, ge=3, le=12)
    style: str = "comic"
    tone: str = "funny"


def _template_script(idea: str, n: int) -> dict[str, Any]:
    """Offline fallback so Studio still works with no LLM configured."""
    beats = ["Meet our hero in a normal moment", "Something strange begins", "It gets worse",
             "The hero reacts", "A bold plan", "Action!", "A twist", "A big finish"]
    panels = []
    for i in range(n):
        beat = beats[min(i, len(beats) - 1)] if i < n - 1 else beats[-1]
        panels.append({
            "scene": f"{idea} — {beat}",
            "caption": beat.upper() + "...",
            "bubbles": [{"side": "left", "text": "Edit me — write the dialogue here."}],
            "sfx": "",
        })
    return {"title": idea[:60], "characters": "", "panels": panels, "source": "template"}


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))


@router.post("/script")
async def make_script(body: ScriptBody) -> dict[str, Any]:
    user = (f"Idea: {body.idea}\nNumber of panels: {body.panels}\n"
            f"Art style: {STYLE_PRESETS.get(body.style, body.style)}\nTone: {body.tone}")
    try:
        raw = await _llm("script", user, _SCRIPT_SYSTEM, 120.0)
        data = _extract_json(raw)
        panels = data.get("panels") or []
        if not panels:
            raise ValueError("empty panels")
        data["panels"] = panels[: body.panels]
        data["source"] = "llm"
        return data
    except (LLMError, ValueError, json.JSONDecodeError) as exc:
        logger.info("studio script fell back to template: %s", exc)
        out = _template_script(body.idea, body.panels)
        out["note"] = f"AI script unavailable ({exc}); edit the panels by hand."
        return out


@router.get("/styles")
def styles() -> dict[str, Any]:
    return {"styles": STYLE_PRESETS, "suffix": NO_TEXT_SUFFIX}


class BubbleIn(BaseModel):
    text: str = ""
    side: str = "left"
    # Optional free placement, percent of the frame (see studio_render).
    x: Optional[float] = None
    y: Optional[float] = None
    w: Optional[float] = None
    tx: Optional[float] = None
    ty: Optional[float] = None


class PanelIn(BaseModel):
    media_id: Optional[str] = None        # generated still
    video_media_id: Optional[str] = None  # optional animated clip
    caption: str = ""
    sfx: str = ""
    sfx_x: Optional[float] = None
    sfx_y: Optional[float] = None
    bubbles: list[BubbleIn] = []


class VoiceIn(BaseModel):
    enabled: bool = False
    engine: str = "edge"
    left: str = ""
    right: str = ""
    speed: float = Field(default=1.0, ge=0.6, le=1.6)


class CaptionsIn(BaseModel):
    style: str = "off"        # off | karaoke | classic
    bubbles: bool = True      # keep the speech bubbles on screen too


class RenderBody(BaseModel):
    panels: list[PanelIn] = Field(min_length=1, max_length=24)
    format: str = "16:9"   # 16:9 | 9:16 | 1:1
    voice: VoiceIn = VoiceIn()
    captions: CaptionsIn = CaptionsIn()


# ── voice ───────────────────────────────────────────────────────────────────

@router.get("/voices")
def voices() -> dict[str, Any]:
    """Voice engines, which can be used right now, and a default man/woman pair for each."""
    out = []
    for e in voice.engine_list():
        left, right = voice.default_voices(e["id"])
        out.append({**e, "default_left": left, "default_right": right})
    stt = subtitles._stt_provider()
    # Best available voice: the user's pick in Engines, else the most natural one they have a key for.
    available = {e["id"] for e in out if e["available"]}
    chosen = engines.assigned_engine(engines._load(), "voice")
    default = chosen if chosen in available else next(
        (e for e in ("elevenlabs", "openai", "edge", "sapi") if e in available), None)
    return {
        "engines": out,
        "default_engine": default,
        "exact_timing": stt[0] if stt else None,   # engine that gives exact subtitle timing, if any
    }


class VoicePreviewBody(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    engine: str = "edge"
    voice: str = ""
    speed: float = Field(default=1.0, ge=0.6, le=1.6)


@router.post("/voice-preview")
async def voice_preview(body: VoicePreviewBody):
    """Speak a short line so the user can hear a voice before choosing it."""
    try:
        path, _used = await asyncio.to_thread(voice.synthesize, body.text, body.engine, body.voice, body.speed)
    except voice.VoiceError as exc:
        raise HTTPException(status_code=502, detail=f"Could not make that voice: {exc}")
    return FileResponse(path, media_type="audio/mpeg" if path.suffix == ".mp3" else "audio/wav")


async def _local_file(media_id: str) -> Path:
    got = await media_service.fetch_and_cache(media_id)
    if not got:
        raise HTTPException(status_code=404, detail=f"media {media_id} not available")
    return got[2]


async def _run_job(job_id: str, panels: list[PanelIn], fmt: str, voice_cfg: dict, captions_cfg: dict) -> None:
    job = _JOBS[job_id]
    try:
        spec: list[dict[str, Any]] = []
        for p in panels:
            item: dict[str, Any] = {
                "caption": p.caption, "sfx": p.sfx, "sfx_x": p.sfx_x, "sfx_y": p.sfx_y,
                "bubbles": [b.model_dump() for b in p.bubbles],
            }
            if p.video_media_id:
                item["clip_path"] = await _local_file(p.video_media_id)
            elif p.media_id:
                item["still_path"] = await _local_file(p.media_id)
            spec.append(item)
        work = STUDIO_DIR / job_id
        out = work / "video.mp4"

        def progress(i: int, n: int) -> None:
            job["progress"] = i / n

        await asyncio.to_thread(studio_render.render_video, spec, out, work, progress, fmt, voice_cfg, captions_cfg)
        job.update(status="done", progress=1.0, file=f"/api/studio/files/{job_id}")
    except Exception as exc:  # surfaced to the UI as a readable error
        logger.exception("studio render failed")
        job.update(status="failed", error=str(exc)[:300])


@router.post("/render")
async def render(body: RenderBody) -> dict[str, str]:
    if studio_render.ffmpeg_path() is None:
        raise HTTPException(status_code=503, detail="ffmpeg is not installed or not on PATH")
    job_id = uuid.uuid4().hex[:12]
    _JOBS[job_id] = {"status": "running", "progress": 0.0}
    asyncio.create_task(_run_job(job_id, body.panels, body.format, body.voice.model_dump(), body.captions.model_dump()))
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def job(job_id: str) -> dict[str, Any]:
    j = _JOBS.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return j


@router.get("/files/{job_id}")
def file(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(status_code=400, detail="bad id")
    path = STUDIO_DIR / job_id / "video.mp4"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not rendered")
    return FileResponse(path, media_type="video/mp4", filename="flowboard-comic.mp4")


# ── AI helpers ──────────────────────────────────────────────────────────────

_FALLBACK_IDEAS = [
    {"title": "Zombie Eclipse", "idea": "Two stickmen survive a zombie outbreak that starts during a solar eclipse"},
    {"title": "Pizza Heist", "idea": "A clumsy stickman crew plans the perfect heist on a pizza shop's secret recipe"},
    {"title": "Robot Best Friend", "idea": "A lonely stickman builds a robot that is far too enthusiastic about everything"},
    {"title": "Haunted Gym", "idea": "A ghost haunts a gym and keeps judging everyone's workout form"},
    {"title": "Dragon Landlord", "idea": "A knight discovers his landlord is a dragon who wants the rent in gold"},
    {"title": "Space Camping", "idea": "Two astronauts go camping on the moon and everything goes wrong"},
]

_IMPROVE_SYSTEM = {
    "idea": "Rewrite the user's story idea into one vivid, funny or exciting sentence (max 30 words) "
            "with a clear hook and stakes. Reply with ONLY the rewritten sentence.",
    "scene": "Rewrite this comic panel description so it is visual, specific and easy to draw: who is "
             "where, what they are doing, the mood and camera angle. Max 40 words, no dialogue, no text "
             "in the picture. Reply with ONLY the description.",
    "dialogue": "Rewrite this comic speech-bubble line so it is punchier, funnier or more dramatic, in the "
                "character's voice. Max 12 words. Reply with ONLY the line, no quotes.",
}


class ImproveBody(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    kind: str = "idea"          # idea | scene | dialogue
    context: str = ""           # e.g. the panel scene, for dialogue


@router.post("/improve")
async def improve(body: ImproveBody) -> dict[str, str]:
    system = _IMPROVE_SYSTEM.get(body.kind)
    if system is None:
        raise HTTPException(status_code=400, detail="kind must be idea, scene or dialogue")
    user = body.text if not body.context else f"Context: {body.context}\nText: {body.text}"
    try:
        out = await _llm("rewrite", user, system, 90.0)
        out = (out or "").strip().strip('"')
        if not out:
            raise ValueError("empty reply")
        return {"text": out}
    except (LLMError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"AI unavailable: {exc}")


class IdeasBody(BaseModel):
    count: int = Field(default=6, ge=1, le=12)
    theme: str = ""


@router.post("/ideas")
async def ideas(body: IdeasBody) -> dict[str, Any]:
    system = ('Reply with ONLY valid JSON: {"ideas": [{"title": str, "idea": str}]}. '
              "Each idea is one fun, specific comic-story premise (max 25 words) with a hook. "
              "Make them very different from each other.")
    user = f"Give {body.count} ideas." + (f" Theme: {body.theme}" if body.theme else "")
    try:
        raw = await _llm("ideas", user, system, 90.0)
        data = _extract_json(raw)
        items = [i for i in (data.get("ideas") or []) if i.get("idea")]
        if not items:
            raise ValueError("no ideas")
        return {"ideas": items[: body.count], "source": "llm"}
    except (LLMError, ValueError, json.JSONDecodeError):
        import random
        pool = _FALLBACK_IDEAS[:]
        random.shuffle(pool)
        return {"ideas": pool[: body.count], "source": "builtin"}
