"""Connectors routes — picture services, publish targets, Google sign-in, and the publish kit.

Credentials go in and are tested and stored locally; only masked hints come back.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from flowboard.routes.studio import STUDIO_DIR, _extract_json, _llm
from flowboard.services import connectors, publish
from flowboard.services.llm.base import LLMError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])


class ValuesBody(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class EnabledBody(BaseModel):
    enabled: bool


class OrderBody(BaseModel):
    order: list[str]


class CustomBody(BaseModel):
    id: Optional[str] = None
    label: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=8, max_length=500)
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = Field(default="", max_length=4000)
    result_kind: str = "url"
    result_path: str = Field(default="", max_length=200)


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
def overview() -> dict[str, Any]:
    return connectors.overview()


@router.put("/order/picture")
def set_order(body: OrderBody) -> dict[str, Any]:
    connectors.set_picture_order(body.order)
    return connectors.overview()


@router.post("/custom")
async def add_custom(body: CustomBody) -> dict[str, Any]:
    try:
        cid = connectors.save_custom(body.model_dump())
        result = await connectors.test_custom(cid)
    except connectors.ConnectorError as exc:
        raise _bad(exc)
    return {"id": cid, "result": result, "overview": connectors.overview()}


@router.put("/{cid}")
async def save(cid: str, body: ValuesBody) -> dict[str, Any]:
    try:
        result = await connectors.save(cid, body.values)
    except connectors.ConnectorError as exc:
        raise _bad(exc)
    return {"result": result, "overview": connectors.overview()}


@router.post("/telegram/find-chat")
async def telegram_find_chat() -> dict[str, Any]:
    try:
        result = await connectors.telegram_find_chat()
    except connectors.ConnectorError as exc:
        raise _bad(exc)
    return {"result": result, "overview": connectors.overview()}


@router.post("/{cid}/test")
async def test(cid: str) -> dict[str, Any]:
    try:
        result = await connectors.retest(cid)
    except connectors.ConnectorError as exc:
        raise _bad(exc)
    return {"result": result, "overview": connectors.overview()}


@router.post("/{cid}/enable")
async def enable(cid: str, body: EnabledBody) -> dict[str, Any]:
    try:
        if body.enabled and cid == "pollinations":
            await connectors.enable_keyless(cid)
        else:
            connectors.set_enabled(cid, body.enabled)
    except connectors.ConnectorError as exc:
        raise _bad(exc)
    return connectors.overview()


@router.delete("/{cid}")
def remove(cid: str) -> dict[str, Any]:
    connectors.remove(cid)
    return connectors.overview()


# ── Google sign-in ──────────────────────────────────────────────────────────

def _redirect(request: Request) -> str:
    # The agent's own port, not the address the browser used (the web app reaches us through a proxy on 5173).
    server = request.scope.get("server")
    port = server[1] if server and server[1] else 8434
    return f"http://127.0.0.1:{port}/api/connectors/google/callback"


@router.post("/{cid}/signin")
def signin(cid: str, request: Request) -> dict[str, str]:
    try:
        return {"url": publish.google_authorize_url(cid, _redirect(request))}
    except publish.PublishError as exc:
        raise _bad(exc)


_PAGE = ("<!doctype html><meta charset=utf-8><title>Flow Studio</title>"
         "<body style=\"font:18px system-ui;display:grid;place-items:center;height:90vh;text-align:center\">"
         "<div><div style=\"font-size:56px\">{icon}</div><h2>{title}</h2><p>{text}</p></div></body>")


@router.get("/google/callback", response_class=HTMLResponse, include_in_schema=False)
async def google_callback(state: str = "", code: str = "", error: str = "") -> HTMLResponse:
    if error or not code:
        return HTMLResponse(_PAGE.format(icon="⚠️", title="Sign-in was cancelled", text="Close this tab and try again in Flow Studio."), 400)
    try:
        await publish.google_finish(state, code)
    except publish.PublishError as exc:
        return HTMLResponse(_PAGE.format(icon="⚠️", title="Sign-in failed", text=str(exc)), 400)
    return HTMLResponse(_PAGE.format(icon="✅", title="Google is connected", text="You can close this tab and go back to Flow Studio."))


# ── publish ─────────────────────────────────────────────────────────────────

pub = APIRouter(prefix="/api/publish", tags=["publish"])


class KitBody(BaseModel):
    title: str = ""
    story: str = Field(default="", max_length=3000)
    platform: str = "youtube"


@pub.post("/kit")
async def kit(body: KitBody) -> dict[str, Any]:
    """Title, description and hashtags for the finished video. Falls back to simple text with no AI."""
    base = body.title.strip() or "My comic story"
    fallback = {"title": base[:90], "description": (body.story.strip() or base)[:300],
                "tags": ["comic", "story", "animation", "stickman"], "source": "builtin"}
    system = ('Reply with ONLY valid JSON: {"title": str, "description": str, "tags": [str]}. '
              "Write a catchy title (max 70 characters, no hashtags), a 1-2 sentence description with a hook, "
              "and 5 short hashtag words without the # sign. Friendly, simple English.")
    try:
        raw = await _llm("rewrite", f"Platform: {body.platform}\nTitle: {base}\nStory: {body.story[:1500]}", system, 60.0)
        data = _extract_json(raw)
        title = str(data.get("title") or "").strip()
        if not title:
            return fallback
        tags = [re.sub(r"\W+", "", str(t)) for t in (data.get("tags") or [])][:8]
        return {"title": title[:95], "description": str(data.get("description") or "")[:600],
                "tags": [t for t in tags if t] or fallback["tags"], "source": "ai"}
    except (LLMError, ValueError, json.JSONDecodeError, AttributeError):
        return fallback


class PublishBody(BaseModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{12}$")
    targets: list[str] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list)
    privacy: str = "private"
    vertical: bool = True


@pub.post("")
async def send(body: PublishBody) -> dict[str, Any]:
    video = STUDIO_DIR / body.job_id / "video.mp4"
    try:
        results = await publish.publish(body.targets, video, title=body.title, description=body.description,
                                        tags=body.tags, privacy=body.privacy, vertical=body.vertical)
    except publish.PublishError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"results": results}
