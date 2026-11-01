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
from flowboard.services import connectors, image_engines, studio_render, subtitles, voice
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
    "comic": "Western comic book illustration, bold black ink outlines, halftone dot shading, flat vivid colors",
    "stickman": "Comic book illustration, bold black ink outlines, halftone shading, simple stickman "
                "characters with round white heads, thin black stick limbs and dot eyes, flat vivid colors",
    "anime": "Anime key visual, clean cel shading, expressive characters, vibrant colors",
    "watercolor": "Soft watercolor illustration, painterly textures, gentle colors",
}
# Studio draws the speech bubbles and captions itself, so the picture must contain no text.
# It must also fill the whole frame: an earlier version asked for an empty band at the top and
# the model drew it as a blank box, which spoiled every picture.
NO_TEXT_SUFFIX = (
    " Full-bleed artwork that fills the entire frame edge to edge, with no panel borders, no frames, "
    "no caption boxes, no empty rectangles and no white or blank bands. "
    "NO text, NO letters, NO speech bubbles, NO words anywhere."
)
# Only for a panel whose script says it needs room for a long speech bubble. The space is part of
# the picture (sky, wall, floor), never a box or a border.
HEADROOM_SUFFIX = (
    " Keep the upper third of the frame as calm, uncluttered background (sky, wall or ceiling) with "
    "nothing important in it, so a speech bubble can sit there. It must be part of the same artwork, "
    "not a box, band or border."
)

_SCRIPT_SYSTEM = (
    "You write short comic-book scripts. Reply with ONLY valid JSON, no markdown, in this shape:\n"
    '{"title": str, "characters": str, "panels": [{"scene": str, "caption": str, '
    '"bubbles": [{"side": "left"|"right", "text": str}], "sfx": str, "space": "none"|"top"}]}\n'
    "Rules: 'characters' is one sentence describing each character's fixed look (reused in every "
    "panel for consistency). 'scene' describes only the visuals of that panel (no text in the "
    "picture). Each bubble text is at most 12 words. At most 2 bubbles per panel; 'left' speaks "
    "first. 'caption' is narration or empty. 'sfx' is a short sound word like BONK! or empty. "
    "'space' is \"none\" for almost every panel: the picture fills the frame and the speech bubbles sit "
    "on top of the art. Use \"top\" ONLY when a panel has long dialogue or two bubbles that would cover "
    "something important, so the picture keeps calm background at the top for them. "
    "Never describe borders, frames, boxes or empty bands in a scene. "
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
            "space": "none",
        })
    return {"title": idea[:60], "characters": "", "panels": panels, "source": "template"}


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))


_FLOW_LINK = re.compile(r"https?://(?:labs\.)?flow\.google(?:\.com)?/\S*?project/([0-9a-fA-F-]{36})\S*", re.I)


def take_flow_link(text: str) -> tuple[str, Optional[str]]:
    """If the text holds a Google Flow project link, pin that project (everything is drawn
    there from now on) and return the text without the link."""
    m = _FLOW_LINK.search(text)
    if not m:
        return text, None
    from flowboard.services.flow_project import set_override
    set_override(m.group(1).lower())
    return re.sub(r"\s{2,}", " ", _FLOW_LINK.sub("", text)).strip(), m.group(1).lower()


@router.post("/script")
async def make_script(body: ScriptBody) -> dict[str, Any]:
    idea, pinned = take_flow_link(body.idea)
    if len(idea) < 3:
        raise HTTPException(status_code=400, detail="Flow project saved. Now say what the story is about, for example: 'Two stickmen survive a zombie eclipse'.")
    body = body.model_copy(update={"idea": idea})
    out = await _make_script(body)
    out["idea"] = idea
    if pinned:
        out["pinned_project"] = pinned
    return out


async def _make_script(body: ScriptBody) -> dict[str, Any]:
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
    return {"styles": STYLE_PRESETS, "suffix": NO_TEXT_SUFFIX, "headroom_suffix": HEADROOM_SUFFIX}


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


# ── pictures from the user's own engines (backup for Flow) ──────────────────

class ImageBody(BaseModel):
    prompt: str = Field(min_length=3, max_length=4000)
    format: str = "16:9"
    ref_media_id: Optional[str] = None   # a picture whose characters the new one must keep


@router.post("/image")
async def make_image(body: ImageBody) -> dict[str, Any]:
    """Draw one picture on a connected engine and cache it like any other media."""
    ref: Optional[bytes] = None
    if body.ref_media_id:
        try:
            ref = (await _local_file(body.ref_media_id)).read_bytes()
        except (HTTPException, OSError):
            ref = None                      # no reference: draw from the text alone
    try:
        data, mime, engine, model = await image_engines.generate(body.prompt, body.format, ref=ref)
    except image_engines.ImageEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    media_id = str(uuid.uuid4())
    if not media_service.ingest_inline_bytes(media_id, data, kind="image", mime=mime):
        raise HTTPException(status_code=500, detail="Could not save the picture.")
    out: dict[str, Any] = {"media_id": media_id, "engine": engine, "model": model, "used_reference": ref is not None}
    if engine == "Cloudflare":
        out["quota"] = await connectors.cloudflare_quota()
    return out


class PictureModelBody(BaseModel):
    model: str


@router.put("/picture-model")
async def set_picture_model(body: PictureModelBody) -> dict[str, Any]:
    """Switch the Cloudflare picture model (9b = best likeness, 4b = many more pictures a day)."""
    try:
        connectors.set_cloudflare_model(body.model)
    except connectors.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"cloudflare": await connectors.cloudflare_quota()}


@router.get("/picture-quota")
async def picture_quota() -> dict[str, Any]:
    """Free pictures left today on Cloudflare, or null when it is not connected."""
    if not connectors.is_ready("cloudflare"):
        return {"cloudflare": None}
    return {"cloudflare": await connectors.cloudflare_quota()}


# ── speech bubble placement ─────────────────────────────────────────────────

class PlaceBubble(BaseModel):
    side: str = "left"
    text: str = ""


class PlaceBody(BaseModel):
    media_id: str
    scene: str = ""
    characters: str = ""
    bubbles: list[PlaceBubble] = Field(default_factory=list, max_length=4)


_PLACE_SYSTEM = (
    "You locate speakers in a comic picture so speech bubbles can point at the right head. "
    "Reply with ONLY JSON: {\"speakers\": [{\"x1\": n, \"y1\": n, \"x2\": n, \"y2\": n}, ...]} with one "
    "entry per bubble, in the same order. Each box is the HEAD of the character who says that bubble, "
    "as percentages 0-100 of the picture width (x) and height (y), origin top-left. Decide who speaks "
    "from the bubble text, the scene and the character list; if unclear, a 'left' bubble belongs to the "
    "left-most character and a 'right' bubble to the right-most one. If the speaker's head is not visible, "
    "box the speaker's body top instead. No other text."
)



def _layout_bubbles(heads: list[tuple[float, float, float, float]]) -> list[dict[str, float]]:
    """Turn head boxes into bubble centre + tail tip. The bubble sits above its speaker's head with
    the tail just over it; when there is no room above, it sits beside the head. Bubbles never overlap,
    and the speaker on the left always gets the left bubble so the tails do not cross."""
    items: list[dict[str, float]] = []
    for x1, y1, x2, y2 in heads:
        x1, x2, top, bottom = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
        hx = (x1 + x2) / 2
        # Boxes often cover only the face, not hair, hood or hat, so keep the tail well clear above it.
        tip_y = max(top - 2.0 - 0.45 * max(bottom - top, 6.0), 3.0)
        items.append({"hx": hx, "x1": x1, "x2": x2, "top": top, "bottom": bottom,
                      "x": hx, "y": tip_y - 14.0, "tx": hx, "ty": tip_y, "side": 0.0})
    mid = sum(it["hx"] for it in items) / len(items) if len(items) > 1 else 50.0
    for it in items:
        it["x"] = max(22.0, min(78.0, it["x"]))
        if it["y"] < 10.0:                          # no room above → put it beside the head
            it["side"] = 1.0
            it["y"] = max(10.0, min(it["top"] + (it["bottom"] - it["top"]) * 0.2, 80.0))
            it["x"] = max(22.0, min(78.0, it["hx"] - 26.0 if it["hx"] < mid else it["hx"] + 26.0))
        it["y"] = max(9.0, min(86.0, it["y"]))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if abs(a["x"] - b["x"]) < 34 and abs(a["y"] - b["y"]) < 18:
                lo, hi = (a, b) if a["hx"] <= b["hx"] else (b, a)
                lo["x"], hi["x"] = min(lo["x"], 30.0), max(hi["x"], 70.0)
                if abs(lo["x"] - hi["x"]) < 34:
                    hi["y"] = min(86.0, lo["y"] + 20.0)
    # A bubble must not cover anyone else's head: slide it sideways, or raise it above that head.
    for it in items:
        for other in items:
            if other is it:
                continue
            covers_x = it["x"] + 17.0 > other["x1"] - 1.0 and it["x"] - 17.0 < other["x2"] + 1.0
            covers_y = it["y"] + 9.0 > other["top"] - 4.0 and it["y"] - 9.0 < other["bottom"]
            if covers_x and covers_y:
                right = other["x2"] + 18.0 if it["hx"] >= other["hx"] else other["x1"] - 18.0
                if 20.0 <= right <= 80.0:
                    it["x"] = right
                else:
                    it["y"] = max(9.0, other["top"] - 14.0)
    for it in items:
        if it["side"]:                              # beside mode: tail touches the edge facing the bubble
            it["tx"] = it["x1"] - 1.5 if it["x"] < it["hx"] else it["x2"] + 1.5
            it["ty"] = it["y"] + 8.0
        it["tx"] = max(2.0, min(98.0, it["tx"]))
        it["ty"] = max(2.0, min(98.0, it["ty"]))
    return [{k: round(it[k], 1) for k in ("x", "y", "tx", "ty")} for it in items]


@router.post("/place-bubbles")
async def place_bubbles(body: PlaceBody) -> dict[str, Any]:
    """Find who is speaking in a picture and return bubble positions that point at their head.
    On any failure the answer is an empty list, so the caller keeps its default positions."""
    if not body.bubbles:
        return {"bubbles": []}
    path = await _local_file(body.media_id)
    listing = "\n".join(f"{i + 1}. [{b.side}] {b.text}" for i, b in enumerate(body.bubbles))
    user = (
        f"Characters: {body.characters or 'not given'}\nScene: {body.scene or 'not given'}\n"
        f"Bubbles, in order:\n{listing}\n\nReturn one head box per bubble."
    )
    try:
        raw = await run_llm("vision", user, system_prompt=_PLACE_SYSTEM, attachments=[str(path.resolve())], timeout=120.0)
        speakers = _extract_json(raw).get("speakers", [])
    except Exception as exc:  # noqa: BLE001 — placement is a nicety; never break drawing over it
        logger.info("place-bubbles: vision unavailable (%s)", exc)
        return {"bubbles": [], "error": str(exc)}
    rows: list[list[float]] = []
    for sp in speakers[: len(body.bubbles)]:
        try:
            rows.append([float(sp[k]) for k in ("x1", "y1", "x2", "y2")])
        except (KeyError, TypeError, ValueError):
            return {"bubbles": [], "error": "unreadable answer"}
    if len(rows) != len(body.bubbles):
        return {"bubbles": [], "error": "wrong number of speakers"}
    # Some vision models answer on a 0-1000 scale instead of 0-100; anything bigger is not a position.
    top = max(max(r) for r in rows)
    if top > 1000 or min(min(r) for r in rows) < 0:
        return {"bubbles": [], "error": "positions out of range"}
    scale = 10.0 if top > 100 else 1.0
    heads = [(r[0] / scale, r[1] / scale, r[2] / scale, r[3] / scale) for r in rows]
    if any(h[2] - h[0] < 1 or h[3] - h[1] < 1 for h in heads) or len(set(heads)) < len(heads):
        return {"bubbles": [], "error": "implausible boxes"}
    return {"bubbles": _layout_bubbles(heads)}


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
    """The picture/clip on disk. Pictures drawn by the user's own engine and some Flow videos are
    saved straight to the cache with no download URL, so look there first."""
    cached = media_service.cached_path(media_id)
    if cached is not None:
        return cached
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
