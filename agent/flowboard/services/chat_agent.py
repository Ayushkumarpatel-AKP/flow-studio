"""Server-side ReAct agent for the bottom-dock chat (E2E video builder).

``POST /api/chat`` persists the user message and schedules
:func:`run_chat_agent` as a FastAPI BackgroundTask. The loop runs HERE,
not in the worker: only LLM reasoning goes through the muse provider
queue (``run_llm("chat_agent", ...)``); every tool is a direct service
call (create node/edge, image/video from Muse or — by default — the
user's Google Flow, ffmpeg concat).

:func:`run_chat_agent` never raises — any failure marks the ChatRun
``failed`` and writes an assistant message carrying the error.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Asset, Board, ChatAttachment, ChatMessage, ChatRun, Edge, Node
from flowboard.services import video_concat
from flowboard.services.activity import record_activity
from flowboard.services.llm.base import LLMError
from flowboard.services.llm.muse import MuseProvider
from flowboard.services.llm.registry import run_llm
from flowboard.services.muse_media import run_muse_media
from flowboard.short_id import generate_unique_short_id

logger = logging.getLogger(__name__)

MAX_TURNS = 12
MAX_GENERATIONS = 6        # pictures + videos per request — each one can spend credits
MAX_NEW_NODES = 8          # a chatty model once created a dozen notes for a "hi"
_MAX_SCRIPT_IN_PROMPT = 12000   # characters of an attached script the agent reads
MAX_FAILS_IN_ROW = 3       # stop and explain instead of retrying the same mistake
_LLM_TIMEOUT_S = 120.0

_NODE_TYPES = {"character", "image", "video", "prompt", "note", "merge"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChatAgentError(Exception):
    """Expected tool failure — fed back to the LLM as an observation."""


class ToolCall(NamedTuple):
    tool: str = ""
    args: dict = {}
    done: bool = False
    reply: str = ""


# ── system prompt ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the Flowboard build agent. You control an infinite-canvas
workspace through tools to fulfil the user's media request end-to-end:
create nodes, generate images/videos, wire edges, and concat videos.

RULES
- Respond with EXACTLY ONE JSON object per turn, no prose outside it.
- Either a tool call: {"tool": "<name>", "args": {...}}
- Or finish: {"done": true, "reply": "<summary for the user>"}
- The reply must be in the same language as the user's message. If unsure, reply in English.
- Node positions: x grows right, y grows down. Lay new content out
  left-to-right starting near x=0, spacing nodes ~300px apart.
- Every node line below says what it HOLDS (a picture, a video or nothing) and what it
  looks like. Use that. To use an existing picture or video, point at its NODE by short
  id: gen_video {"source_node_short_id": "abcd"}, gen_image {"reference_node_short_ids": [...]}.
  NEVER invent asset ids and never ask the user for them.
- If the user is only chatting or asking a question, answer at once with
  {"done": true, "reply": "..."}. Create nothing.
- A #mention marked UNKNOWN is not on this board. Do not guess and do not create anything:
  reply with done and say it was not found.
- Do exactly what was asked, then finish. As soon as the request is satisfied, reply
  {"done": true, "reply": "..."}. Never repeat a tool call that already succeeded.
- A node that already holds a picture/video is the user's work. Do not target it unless they
  asked to redo it (then pass "replace": true). Otherwise leave node_short_id out and a new
  node is created.
- Keep prompts you send to gen_image/gen_video concrete and visual.
- Attached images are listed under "Attached images" with an asset id. Use them directly:
  gen_image {"reference_asset_ids": [<asset id>]} to make a new picture from one, or
  gen_video {"source_asset_id": <asset id>} to animate one. Look at the attached picture first
  and describe its subject in your prompts so the result keeps the same person/character.
- An attached script is the story to build. Read all of it, split it into its scenes, then make
  one picture per scene with gen_image (describing the same characters the same way each time,
  and using any attached picture as reference), and a video per picture only if they asked
  for video. Do not ask the user to paste the script again.
- "Take two of this picture" / "make 2 versions" means TWO separate gen_image calls that both use the
  attached asset as reference, each with a different cinematic look (for example one wide
  golden-hour shot and one dramatic close-up with moody lighting). "Cinematic" means film-still
  lighting, depth of field, 35mm lens, colour grade. If they also ask for a video, make one
  gen_video per picture, starting from the picture node you just made.
- If a tool returns an error, adapt (fix args, try another tool) instead
  of repeating the identical call.

TOOLS
- create_node: {"type": "character|image|video|prompt|note|merge",
  "x": float, "y": float, "data": {optional dict}}
  → {"ok": true, "short_id": "abcd", "id": 7}
- add_edge: {"from_short_id": "abcd", "to_short_id": "efgh",
  "kind": "ref"}  (kind: "ref" = use as reference)
- gen_image: {"prompt": "...", "node_short_id": "abcd" (optional), "replace": true (only to
  redo a node that already holds a result), "reference_node_short_ids": ["efgh"] (optional:
  nodes whose picture should guide the new one), "reference_asset_ids": [1] (optional: only
  asset ids that appear in the node list or attachments)}
  Generates an image and binds it to the node (creates an image node if none given).
- gen_video: {"prompt": "...", "source_node_short_id": "abcd" (the picture node used as the
  start frame; or "source_asset_id": 3 from the node list), "node_short_id": "efgh"
  (optional), "replace": true (optional)}  Generates a video and binds it to a video node.
- concat_videos: {"video_node_short_ids": ["abcd", "efgh"] (or "video_asset_ids": [4, 5]),
  "node_short_id": "ijkl" (optional)}  Joins videos in order with ffmpeg; binds the result
  to a merge node (created if none given).
- update_node: {"short_id": "abcd", "data_patch": {...}}  Shallow-merges
  data (a null value deletes the key).
- refresh: {}  Re-reads the board (nodes/edges) after many mutations.

Typical video job: upload/start images → create_node image ×N →
gen_image each (or reuse attached images directly) → gen_video per image
with source_asset_id → create_node merge → add_edge video→merge in order
→ concat_videos → done with a short summary.
"""


# ── context builder ───────────────────────────────────────────────────

def _media_ids(data: Optional[dict]) -> list[str]:
    d = data or {}
    out: list[str] = []
    for m in [d.get("mediaId"), *(d.get("mediaIds") or [])]:
        if isinstance(m, str) and m and m not in out:
            out.append(m)
    return out


def _asset_map(s, nodes) -> dict[str, tuple[int, str]]:
    """media uuid -> (Asset id, kind) for every picture/video the nodes hold."""
    mids = {m for n in nodes for m in _media_ids(n.data)}
    if not mids:
        return {}
    rows = s.exec(select(Asset).where(Asset.uuid_media_id.in_(list(mids)))).all()
    return {r.uuid_media_id: (r.id, r.kind or "") for r in rows}


def _describe(n, assets: dict[str, tuple[int, str]]) -> str:
    """One line saying what a node holds, so the agent can recognise the user's own work."""
    d = n.data or {}
    bits: list[str] = []
    if d.get("title"):
        bits.append(f"title={str(d['title'])[:60]!r}")
    if d.get("prompt"):
        bits.append(f"prompt={str(d['prompt'])[:120]!r}")
    if d.get("aiBrief"):
        bits.append(f"looks_like={str(d['aiBrief'])[:140]!r}")
    mids = _media_ids(d)
    if mids:
        kind = "VIDEO" if n.type in ("video", "merge") else "PICTURE"
        aid = assets.get(mids[0], (None, ""))[0]
        bits.append(f"HOLDS_{kind}" + (f"(asset={aid})" if aid else "") + (f" x{len(mids)}" if len(mids) > 1 else ""))
    else:
        bits.append("EMPTY")
    return " ".join(bits)


def build_board_context(
    board_id: int,
    message: str,
    attachment_asset_ids: list[int],
    mentions: list[str],
) -> str:
    """Static per-run context: history + current request + attachments."""
    with get_session() as s:
        board = s.get(Board, board_id)
        hist = s.exec(
            select(ChatMessage)
            .where(ChatMessage.board_id == board_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(20)
        ).all()
        hist = list(reversed(hist))
        att_counts: dict[int, int] = {}
        if hist:
            rows = s.exec(
                select(ChatAttachment).where(
                    ChatAttachment.message_id.in_([m.id for m in hist if m.id])
                )
            ).all()
            for r in rows:
                att_counts[r.message_id] = att_counts.get(r.message_id, 0) + 1
        nodes = s.exec(select(Node).where(Node.board_id == board_id)).all()
        node_by_short = {n.short_id: n for n in nodes}
        assets = _asset_map(s, nodes)

    lines = [f"# Board: {board.name if board else board_id} (id={board_id})"]
    lines.append("## Recent chat (oldest → newest)")
    if not hist:
        lines.append("(no previous messages)")
    for m in hist:
        content = (m.content or "")[:600]
        suffix = ""
        n_att = att_counts.get(m.id or -1, 0)
        if n_att:
            suffix = f" [{n_att} image(s) attached]"
        lines.append(f"[{m.role}] {content}{suffix}")
    # Every #xxxx the user typed, whether or not the UI recognised it as a node.
    typed = list(dict.fromkeys([*mentions, *re.findall(r"#([A-Za-z0-9]{4})\b", message)]))
    if typed:
        lines.append("## Mentions")
        for men in typed:
            n = node_by_short.get(men)
            if n is not None:
                lines.append(f"#{men} → {n.type} node {_describe(n, assets)}")
            else:
                lines.append(f"#{men} → UNKNOWN: there is no such node on this board. Do not guess; tell the user.")
    lines.append("## Current request")
    lines.append(message)
    if attachment_asset_ids:
        with get_session() as s:
            paths = []
            scripts = []
            for aid in attachment_asset_ids:
                row = s.get(Asset, aid)
                if row is None or not row.local_path:
                    continue
                if (row.kind or "") == "script":
                    try:
                        body = Path(row.local_path).read_text(encoding="utf-8")[:_MAX_SCRIPT_IN_PROMPT]
                    except OSError:
                        continue
                    name = Path(row.local_path).stem.split("__", 1)[-1]
                    scripts.append(f"--- script file: {name} ---\n{body}\n--- end of {name} ---")
                else:
                    paths.append(f"asset {aid}: {row.local_path}")
        if paths:
            lines.append(
                "Attached images (asset id → absolute path, readable by your tools):\n"
                + "\n".join(paths)
            )
        if scripts:
            lines.append("Attached scripts (the full text is below; follow it):\n" + "\n\n".join(scripts))
    lines.append(_dynamic_context(board_id))
    return "\n".join(lines)


def _dynamic_context(board_id: int) -> str:
    """Fresh nodes/edges snapshot — rebuilt every turn."""
    with get_session() as s:
        nodes = s.exec(
            select(Node).where(Node.board_id == board_id).order_by(Node.id)
        ).all()
        edges = s.exec(
            select(Edge).where(Edge.board_id == board_id).order_by(Edge.id)
        ).all()
        by_id = {n.id: n for n in nodes}
        assets = _asset_map(s, nodes)
    lines = ["## Nodes"]
    if not nodes:
        lines.append("(board is empty)")
    for n in nodes:
        lines.append(
            f"#{n.short_id} [{n.type}] status={n.status} "
            f"@({n.x:.0f},{n.y:.0f}) {_describe(n, assets)}".rstrip()
        )
    lines.append("## Edges")
    for e in edges:
        a = by_id.get(e.source_id)
        b = by_id.get(e.target_id)
        if a and b:
            lines.append(f"#{a.short_id} -> #{b.short_id} ({e.kind})")
    return "\n".join(lines)


# ── LLM call (registry with muse fallback) ────────────────────────────

async def _chat_llm(
    user_prompt: str,
    attachments: Optional[list[str]],
    turn: int,
) -> str:
    """Route through ``run_llm("chat_agent")``, which tries the user's own keys
    from the Engines screen first. With nothing connected and no provider pinned,
    fall back to Muse if it happens to be running; otherwise say what to do."""
    try:
        return await run_llm(
            "chat_agent",
            user_prompt,
            system_prompt=_SYSTEM_PROMPT,
            attachments=attachments,
            timeout=_LLM_TIMEOUT_S,
        )
    except LLMError as exc:
        if "No AI provider configured for chat_agent" not in str(exc):
            raise
        logger.info("chat_agent: no provider pinned, falling back to muse")
        provider = MuseProvider()
        if not await provider.is_available():
            raise LLMError(
                "The chat agent has no AI engine to think with yet. Open 🔌 Engines and add a "
                "free Groq key (no card needed), then send your message again."
            )
        return await provider.run(
            user_prompt,
            system_prompt=_SYSTEM_PROMPT,
            attachments=attachments,
            timeout=_LLM_TIMEOUT_S,
        )


# ── media generation (Muse if set up, otherwise Google Flow) ──────────

async def _muse_ready() -> bool:
    try:
        return await MuseProvider().is_available()
    except Exception:  # never let a probe break a chat run
        return False


async def _flow_media(
    kind: str,
    prompt: str,
    reference_media_ids: Optional[list[str]],
    source_media_ids: Optional[list[str]],
) -> tuple[dict, Optional[str]]:
    """Make an image/video through the same Flow worker the canvas buttons use."""
    from flowboard.db.models import Request
    from flowboard.services.flow_project import effective_project_id
    from flowboard.worker.processor import get_worker

    project_id = effective_project_id()
    if not project_id:
        return {}, ("No Flow project is linked yet. Open a project in your Flow tab "
                    "(the extension links it automatically) and try again.")
    if kind == "image":
        rtype = "gen_image"
        params: dict[str, Any] = {
            "prompt": prompt, "project_id": project_id,
            "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE", "variant_count": 1,
        }
        if reference_media_ids:
            params["ref_media_ids"] = reference_media_ids
    else:
        if not source_media_ids:
            return {}, "A start frame is required to make a video."
        rtype = "gen_video"
        params = {
            "prompt": prompt, "project_id": project_id, "start_media_id": source_media_ids[0],
            "aspect_ratio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
        }
    with get_session() as s:
        req = Request(type=rtype, params=params, status="queued")
        s.add(req)
        s.commit()
        s.refresh(req)
        rid = req.id
    get_worker().enqueue(rid)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        with get_session() as s:
            row = s.get(Request, rid)
            status, result, error = row.status, row.result, row.error
        if status == "done":
            return (result or {}), None
        if status in ("failed", "canceled", "timeout"):
            return {}, error or status
    return {}, "Timed out waiting for Google Flow."


_FLOW_HINT = ("No image/video engine is ready. Open a project in your Google Flow tab "
              "(the extension links it automatically), keep it in front, and try again.")


def _flow_ready() -> bool:
    from flowboard.services.flow_project import effective_project_id
    return bool(effective_project_id())


async def _generate_media(
    kind: str,
    prompt: str,
    *,
    reference_media_ids: Optional[list[str]] = None,
    source_media_ids: Optional[list[str]] = None,
) -> tuple[dict, Optional[str]]:
    """Google Flow when a project is linked (and Muse is not set up), else Muse."""
    flow = _flow_ready()
    if flow and not await _muse_ready():
        return await _flow_media(kind, prompt, reference_media_ids, source_media_ids)
    if kind == "image":
        result, error = await run_muse_media(kind="image", prompt=prompt, reference_media_ids=reference_media_ids)
    else:
        result, error = await run_muse_media(kind="video", prompt=prompt, source_media_ids=source_media_ids)
    if error and not flow and "muse" in error.lower():
        error = _FLOW_HINT   # the person never set Muse up; point at what they actually use
    return result, error


# ── tool-call parsing ─────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _first_json_object(text: str) -> Any:
    """The first complete JSON object in ``text``, ignoring chatter before or after it."""
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _end = dec.raw_decode(text[i:])
            return obj
        except ValueError:
            i = text.find("{", i + 1)
    return None


def _parse_tool_call(raw: str) -> Optional[ToolCall]:
    text = (raw or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    obj = _first_json_object(text)
    if not isinstance(obj, dict):
        return None
    if obj.get("done") is True or str(obj.get("done")).lower() == "true":
        reply = obj.get("reply", "")
        return ToolCall(done=True, reply=reply if isinstance(reply, str) else "")
    tool = obj.get("tool") or obj.get("name") or obj.get("action")
    args = obj.get("args", obj.get("arguments", obj.get("parameters")))
    if args is None:
        # Weaker models put the arguments next to "tool" instead of inside "args".
        args = {k: v for k, v in obj.items() if k not in ("tool", "name", "action", "args", "arguments", "parameters")}
    if not isinstance(tool, str) or not tool or not isinstance(args, dict):
        return None
    return ToolCall(tool=tool, args=args)


# ── tool implementations ──────────────────────────────────────────────

def _node_by_short(s, board_id: int, short_id: str) -> Optional[Node]:
    return s.exec(
        select(Node).where(Node.board_id == board_id, Node.short_id == short_id)
    ).first()


def _auto_pos(s, board_id: int) -> tuple[float, float]:
    nodes = s.exec(select(Node).where(Node.board_id == board_id)).all()
    if not nodes:
        return 0.0, 0.0
    return max(n.x for n in nodes) + 300.0, 0.0


def _bind_media(node_id: int, media_ids: list[str], *, newest_first: bool = True) -> None:
    """Mirror the pipeline executor's binding: data.mediaIds + mediaId, status → done.

    The new result goes first, so "regenerate" shows the new picture instead of
    silently keeping the old one on top."""
    with get_session() as s:
        node = s.get(Node, node_id)
        if node is None:
            return
        data = dict(node.data or {})
        existing = [m for m in (data.get("mediaIds") or []) if isinstance(m, str)]
        fresh = [m for m in media_ids if m]
        merged = (fresh + [m for m in existing if m not in fresh]) if newest_first \
            else (existing + [m for m in fresh if m not in existing])
        data["mediaIds"] = merged
        if merged:
            data["mediaId"] = merged[0]
        node.data = data
        node.status = "done"
        s.add(node)
        s.commit()


def _node_media(s, board_id: int, short_id: str, kind: str) -> tuple[Node, str]:
    """The node and its active media id, with a message the agent can act on."""
    node = _node_by_short(s, board_id, str(short_id))
    if node is None:
        raise ChatAgentError(f"node #{short_id} is not on this board — use a short id from the node list; do not guess")
    mids = _media_ids(node.data)
    if not mids:
        raise ChatAgentError(f"node #{short_id} holds nothing yet; generate into it first or pick another node")
    is_video = node.type in ("video", "merge")
    if kind == "picture" and is_video:
        raise ChatAgentError(f"node #{short_id} holds a video, but a picture is needed here")
    if kind == "video" and not is_video:
        raise ChatAgentError(f"node #{short_id} holds a picture, not a video")
    return node, mids[0]


def _guard_overwrite(s, board_id: int, short_id: Optional[str], replace: bool) -> None:
    """Refuse to put a result into a node that already holds the user's work, unless asked."""
    if not short_id:
        return
    node = _node_by_short(s, board_id, str(short_id))
    if node is None:
        raise ChatAgentError(f"node #{short_id} is not on this board — use a short id from the node list")
    if _media_ids(node.data) and not replace:
        raise ChatAgentError(
            f"node #{short_id} already holds a result. Leave node_short_id out to make a new node, "
            f'or set "replace": true only if the user asked to redo that one.'
        )


def _get_or_create_media_node(
    s, board_id: int, short_id: Optional[str], node_type: str, prompt: str
) -> Node:
    if short_id:
        node = _node_by_short(s, board_id, short_id)
        if node is None:
            raise ChatAgentError(f"node #{short_id} not found on this board")
        return node
    x, y = _auto_pos(s, board_id)
    node = Node(
        board_id=board_id,
        short_id=generate_unique_short_id(s, board_id),
        type=node_type,
        x=x,
        y=y,
        data={"prompt": prompt} if prompt else {},
        status="idle",
    )
    s.add(node)
    s.commit()
    s.refresh(node)
    return node


def _asset_media_id(s, asset_id: int, expect_kind: Optional[str] = None) -> str:
    row = s.get(Asset, asset_id)
    if row is None:
        raise ChatAgentError(f"asset {asset_id} not found")
    if expect_kind and (row.kind or "") != expect_kind:
        raise ChatAgentError(
            f"asset {asset_id} has kind {row.kind!r}, expected {expect_kind!r}"
        )
    if not row.uuid_media_id:
        raise ChatAgentError(f"asset {asset_id} has no media id")
    return row.uuid_media_id


_FLOW_UPLOADS: dict[tuple[int, str], str] = {}   # (chat asset id, flow project) -> media id inside that project


async def _resolve_asset_media(asset_id: int, expect_kind: Optional[str] = None) -> str:
    """The media id to hand to the picture/video engine for an asset.

    A picture the user attached in chat only exists on this computer under a made-up id. Google
    Flow does not know that id, so it is uploaded to the linked Flow project first and the id
    Flow returns is used instead. Muse reads the local file itself, so it keeps the local id."""
    with get_session() as s:
        mid = _asset_media_id(s, asset_id, expect_kind)
        row = s.get(Asset, asset_id)
        path, mime = (row.local_path, row.mime) if row else (None, None)
    if not path or "chat_uploads" not in str(path):
        return mid
    if not _flow_ready() or await _muse_ready():
        return mid
    from fastapi import HTTPException
    from flowboard.routes.upload import _ingest_image_bytes
    from flowboard.services.flow_project import effective_project_id
    project_id = effective_project_id()
    if (asset_id, project_id) in _FLOW_UPLOADS:
        return _FLOW_UPLOADS[(asset_id, project_id)]
    try:
        raw = Path(path).read_bytes()
        out = await _ingest_image_bytes(raw, mime or "image/jpeg", project_id, Path(path).name, None)
    except (OSError, HTTPException) as exc:
        detail = getattr(exc, "detail", exc)
        raise ChatAgentError(f"could not upload attached picture {asset_id} to Google Flow: {detail}")
    _FLOW_UPLOADS[(asset_id, project_id)] = out["media_id"]
    return out["media_id"]


async def _tool_create_node(board_id: int, args: dict) -> dict:
    ntype = args.get("type")
    if ntype not in _NODE_TYPES:
        raise ChatAgentError(
            f"unknown node type {ntype!r}; valid: {sorted(_NODE_TYPES)}"
        )
    data = args.get("data") or {}
    if not isinstance(data, dict):
        raise ChatAgentError("data must be an object")
    try:
        x = float(args.get("x", 0.0))
        y = float(args.get("y", 0.0))
    except (TypeError, ValueError):
        raise ChatAgentError("x/y must be numbers")
    with get_session() as s:
        if "x" not in args or "y" not in args:
            x, y = _auto_pos(s, board_id)
        node = Node(
            board_id=board_id,
            short_id=generate_unique_short_id(s, board_id),
            type=ntype,
            x=x,
            y=y,
            data=data,
            status="idle",
        )
        s.add(node)
        s.commit()
        s.refresh(node)
        return {
            "ok": True,
            "tool": "create_node",
            "id": node.id,
            "short_id": node.short_id,
            "type": ntype,
        }


async def _tool_add_edge(board_id: int, args: dict) -> dict:
    from_sid = args.get("from_short_id")
    to_sid = args.get("to_short_id")
    kind = args.get("kind", "ref")
    if not from_sid or not to_sid:
        raise ChatAgentError("from_short_id and to_short_id are required")
    with get_session() as s:
        a = _node_by_short(s, board_id, from_sid)
        b = _node_by_short(s, board_id, to_sid)
        if a is None:
            raise ChatAgentError(f"node #{from_sid} not found")
        if b is None:
            raise ChatAgentError(f"node #{to_sid} not found")
        edge = Edge(board_id=board_id, source_id=a.id, target_id=b.id, kind=kind)
        s.add(edge)
        s.commit()
        s.refresh(edge)
        return {
            "ok": True,
            "tool": "add_edge",
            "id": edge.id,
            "from": from_sid,
            "to": to_sid,
        }


async def _tool_gen_image(board_id: int, args: dict) -> dict:
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ChatAgentError("prompt is required")
    ref_ids = args.get("reference_asset_ids") or []
    ref_nodes = args.get("reference_node_short_ids") or []
    if not isinstance(ref_ids, list) or not isinstance(ref_nodes, list):
        raise ChatAgentError("reference_asset_ids and reference_node_short_ids must be lists")
    target = args.get("node_short_id")
    with get_session() as s:
        _guard_overwrite(s, board_id, target, bool(args.get("replace")))
        node_mids = [_node_media(s, board_id, sid, "picture")[1] for sid in ref_nodes]
    ref_mids = [await _resolve_asset_media(int(aid), "image") for aid in ref_ids] + node_mids
    result, error = await _generate_media("image", prompt, reference_media_ids=ref_mids or None)
    if error:
        return {"ok": False, "tool": "gen_image", "error": error}
    out = [m for m in (result.get("media_ids") or []) if m]
    if not out:
        return {"ok": False, "tool": "gen_image", "error": "worker returned no media"}
    with get_session() as s:
        node = _get_or_create_media_node(s, board_id, target, "image", prompt)
        node_id, short_id = node.id, node.short_id
    _bind_media(node_id, out)
    return {"ok": True, "tool": "gen_image", "node_short_id": short_id, "media_ids": out}


async def _tool_gen_video(board_id: int, args: dict) -> dict:
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ChatAgentError("prompt is required")
    source_node = args.get("source_node_short_id")
    source_aid = args.get("source_asset_id")
    if source_node is None and source_aid is None:
        raise ChatAgentError(
            "say which picture to start from: source_node_short_id (a node that holds a picture) "
            "or source_asset_id from the node list"
        )
    target = args.get("node_short_id")
    with get_session() as s:
        _guard_overwrite(s, board_id, target, bool(args.get("replace")))
        src_mid = _node_media(s, board_id, source_node, "picture")[1] if source_node is not None else None
    if src_mid is None:
        src_mid = await _resolve_asset_media(int(source_aid))
    result, error = await _generate_media("video", prompt, source_media_ids=[src_mid])
    if error:
        return {"ok": False, "tool": "gen_video", "error": error}
    out = [m for m in (result.get("media_ids") or []) if m]
    if not out:
        return {"ok": False, "tool": "gen_video", "error": "worker returned no media"}
    with get_session() as s:
        node = _get_or_create_media_node(s, board_id, target, "video", prompt)
        node_id, short_id = node.id, node.short_id
    _bind_media(node_id, out)
    return {"ok": True, "tool": "gen_video", "node_short_id": short_id, "media_ids": out}


async def _tool_concat_videos(board_id: int, args: dict) -> dict:
    aids = list(args.get("video_asset_ids") or [])
    nodes = args.get("video_node_short_ids") or []
    if not isinstance(aids, list) or not isinstance(nodes, list):
        raise ChatAgentError("video_asset_ids and video_node_short_ids must be lists")
    with get_session() as s:
        for sid in nodes:
            _node, mid = _node_media(s, board_id, sid, "video")
            row = s.exec(select(Asset).where(Asset.uuid_media_id == mid)).first()
            if row is None:
                raise ChatAgentError(f"the video in node #{sid} has not been saved on this computer yet")
            aids.append(row.id)
    if not aids:
        raise ChatAgentError("say which videos to join: video_node_short_ids or video_asset_ids")
    new_id, error = await asyncio.to_thread(
        video_concat.concat_video_assets, [int(a) for a in aids]
    )
    if error or new_id is None:
        return {"ok": False, "tool": "concat_videos", "error": error or "unknown"}
    with get_session() as s:
        node = _get_or_create_media_node(
            s, board_id, args.get("node_short_id"), "merge", "concat"
        )
        node_id, short_id = node.id, node.short_id
        row = s.get(Asset, new_id)
        out_mids = [row.uuid_media_id] if row and row.uuid_media_id else []
    if out_mids:
        _bind_media(node_id, out_mids)
    return {"ok": True, "tool": "concat_videos", "asset_id": new_id, "node_short_id": short_id}


async def _tool_update_node(board_id: int, args: dict) -> dict:
    short_id = args.get("short_id")
    patch = args.get("data_patch") or {}
    if not short_id:
        raise ChatAgentError("short_id is required")
    if not isinstance(patch, dict):
        raise ChatAgentError("data_patch must be an object")
    with get_session() as s:
        node = _node_by_short(s, board_id, short_id)
        if node is None:
            raise ChatAgentError(f"node #{short_id} not found")
        # Same shallow-merge + null-deletes semantics as PATCH /api/nodes.
        merged = dict(node.data or {})
        for dk, dv in patch.items():
            if dv is None:
                merged.pop(dk, None)
            else:
                merged[dk] = dv
        node.data = merged
        s.add(node)
        s.commit()
        return {"ok": True, "tool": "update_node", "short_id": short_id}


async def _tool_refresh(board_id: int, args: dict) -> dict:
    return {"ok": True, "tool": "refresh", "board": _dynamic_context(board_id)}


_TOOLS = {
    "create_node": _tool_create_node,
    "add_edge": _tool_add_edge,
    "gen_image": _tool_gen_image,
    "gen_video": _tool_gen_video,
    "concat_videos": _tool_concat_videos,
    "update_node": _tool_update_node,
    "refresh": _tool_refresh,
}


async def _execute_tool(board_id: int, tool: str, args: dict) -> dict:
    handler = _TOOLS.get(tool)
    if handler is None:
        return {
            "ok": False,
            "tool": tool,
            "error": f"unknown tool {tool!r}; available: {sorted(_TOOLS)}",
        }
    try:
        obs = await handler(board_id, args)
    except ChatAgentError as exc:
        return {"ok": False, "tool": tool, "error": str(exc)}
    obs.setdefault("tool", tool)
    return obs


# ── run lifecycle ─────────────────────────────────────────────────────

def _finish_run(run_id: int, reply: str, *, failed: bool = False) -> None:
    with get_session() as s:
        run = s.get(ChatRun, run_id)
        if run is None:
            logger.error("chat_agent: ChatRun %s vanished", run_id)
            return
        content = reply.strip() or "(agent finished with no summary)"
        msg = ChatMessage(
            board_id=run.board_id, role="assistant", content=content, mentions=[]
        )
        s.add(msg)
        run.status = "failed" if failed else "done"
        if failed:
            run.error = content[:500]
        run.finished_at = _utcnow()
        s.add(run)
        s.commit()


async def _run_agent(
    board_id: int,
    run_id: int,
    message: str,
    attachment_asset_ids: list[int],
    mentions: list[str],
) -> None:
    static_ctx = build_board_context(board_id, message, attachment_asset_ids, mentions)

    attach_paths: Optional[list[str]] = None
    if attachment_asset_ids:
        with get_session() as s:
            paths = []
            for aid in attachment_asset_ids:
                row = s.get(Asset, aid)
                if row is not None and (row.kind or "") == "image" and row.local_path and Path(row.local_path).is_file():
                    paths.append(str(Path(row.local_path).resolve()))
        attach_paths = paths or None

    allow_replace = bool(_REDO_WORDS.search(message))
    observations: list[dict] = []
    succeeded: set[str] = set()      # exact calls that already worked
    generated_into: set[tuple[str, str]] = set()   # (tool, node) pairs already produced in this request
    generations = new_nodes = fails_in_row = repeats = 0
    for turn in range(MAX_TURNS):
        dynamic = _dynamic_context(board_id)
        obs_text = ""
        if observations:
            obs_text = "\n\n## Tool results so far\n" + json.dumps(
                observations, ensure_ascii=False, indent=1
            )[-6000:]
            ok_steps = [o.get("tool") for o in observations if o.get("ok")]
            if ok_steps:
                obs_text += (
                    f"\n\n## Progress\n{len(ok_steps)} step(s) already done ({', '.join(map(str, ok_steps))}). "
                    "If the user's request is now satisfied, reply with done. Do not repeat finished steps."
                )
        user_prompt = (
            f"{static_ctx}\n\n{dynamic}{obs_text}\n\n"
            "Respond with exactly one JSON object: a tool call or "
            '{"done": true, "reply": "..."}.'
        )
        # Attachments go to the worker on the first turn only; later turns
        # reference them by asset id (already in context).
        raw = await _chat_llm(
            user_prompt,
            attachments=attach_paths if turn == 0 else None,
            turn=turn,
        )
        call = _parse_tool_call(raw)
        if call is None:
            async with record_activity(
                "chat_agent",
                params={"board_id": board_id, "run_id": run_id, "turn": turn,
                        "tool": "unparseable"},
            ) as act:
                act.set_result({"ok": False})
            observations.append(
                {
                    "ok": False,
                    "error": "could not parse a tool call from your reply; "
                    "reply with exactly one JSON object",
                    "raw": raw[:500],
                }
            )
            fails_in_row += 1
            if fails_in_row >= MAX_FAILS_IN_ROW:
                _finish_run(run_id, _stopped_message(observations, "I could not understand the AI's answers"), failed=not _any_ok(observations))
                return
            continue
        if call.done:
            _finish_run(run_id, call.reply or "Done.")
            return

        if call.args.get("replace") and not allow_replace:
            call.args.pop("replace")    # the user did not ask to redo anything; keep their result
        signature = call.tool + json.dumps(call.args, sort_keys=True, default=str)
        target = call.args.get("node_short_id")
        # A model that is unhappy with its own result keeps re-drawing into the same node with a
        # slightly different prompt. One result per node per request: the user can ask again.
        already_made = call.tool in ("gen_image", "gen_video") and target and (call.tool, str(target)) in generated_into
        if signature in succeeded or already_made:
            # The model forgot it already did this. Do not spend credits twice.
            repeats += 1
            observations.append({
                "ok": False, "tool": call.tool,
                "error": "That step already worked in this request" + (f" (node #{target} was just generated)" if already_made else "")
                         + ". Do not repeat it. If the request is done, reply {\"done\": true, \"reply\": \"...\"}.",
            })
            if repeats >= 2:
                _finish_run(run_id, _auto_summary(observations))
                return
            continue
        if call.tool in ("gen_image", "gen_video") and generations >= MAX_GENERATIONS:
            _finish_run(run_id, _stopped_message(observations, f"I stopped after {MAX_GENERATIONS} pictures/videos to protect your credits"))
            return
        if call.tool == "create_node" and new_nodes >= MAX_NEW_NODES:
            _finish_run(run_id, _stopped_message(observations, f"I stopped after {MAX_NEW_NODES} new nodes"))
            return

        # One activity row per turn so the feed shows live progress.
        redacted = {
            k: (str(v)[:200] if k != "data_patch" else "{...}")
            for k, v in call.args.items()
        }
        async with record_activity(
            "chat_agent",
            params={"board_id": board_id, "run_id": run_id, "turn": turn,
                    "tool": call.tool, "args": redacted},
        ) as act:
            obs = await _execute_tool(board_id, call.tool, call.args)
            act.set_result({"ok": bool(obs.get("ok")), "tool": call.tool})
        observations.append(obs)
        if obs.get("ok"):
            succeeded.add(signature)
            if call.tool in ("gen_image", "gen_video") and target:
                generated_into.add((call.tool, str(target)))
            fails_in_row = 0
            generations += call.tool in ("gen_image", "gen_video")
            new_nodes += call.tool == "create_node"
        else:
            fails_in_row += 1
            if fails_in_row >= MAX_FAILS_IN_ROW:
                _finish_run(run_id, _stopped_message(observations, f"I could not finish: {obs.get('error', 'a step failed')}"),
                            failed=not _any_ok(observations))
                return

    _finish_run(run_id, _stopped_message(observations, "I ran out of steps"), failed=not _any_ok(observations))


# Words that mean "redo that one". Without them a node's existing result is the user's work and
# stays untouched, even if the model asks to replace it.
_REDO_WORDS = re.compile(
    r"regenerat|re-?do|re-?draw|re-?make|again|replace|change|update|tweak|fix|improve|darker|brighter|"
    r"different|another version|new version|variation|variant",
    re.I,
)


_STEP_WORDS = {
    "gen_image": "drew a picture", "gen_video": "made a video", "create_node": "added a node",
    "add_edge": "connected nodes", "concat_videos": "joined the videos", "update_node": "updated a node",
}


def _any_ok(observations: list[dict]) -> bool:
    return any(o.get("ok") for o in observations)


def _auto_summary(observations: list[dict]) -> str:
    """A plain summary of what actually happened, for when the model never says done."""
    counts: dict[str, int] = {}
    for o in observations:
        if o.get("ok"):
            sid = o.get("node_short_id") or o.get("short_id")
            phrase = _STEP_WORDS.get(str(o.get("tool")), str(o.get("tool"))) + (f" in #{sid}" if sid else "")
            counts[phrase] = counts.get(phrase, 0) + 1
    steps = [f"{ph} (×{n})" if n > 1 else ph for ph, n in counts.items()]
    return ("Done — I " + ", then ".join(steps) + ".") if steps else "Done."


def _stopped_message(observations: list[dict], reason: str) -> str:
    if _any_ok(observations):
        return f"{_auto_summary(observations)} {reason}, so I stopped there. Tell me if anything is missing."
    return f"{reason}. Nothing was changed. Try rephrasing, or mention the node with its #id."


async def run_chat_agent(
    board_id: int,
    run_id: int,
    message: str,
    attachment_asset_ids: list[int],
    mentions: list[str],
) -> None:
    """Background-task entry. Never raises."""
    try:
        await _run_agent(board_id, run_id, message, attachment_asset_ids, mentions)
    except LLMError as exc:
        logger.warning("chat_agent run %s: llm unavailable (%s)", run_id, exc)
        _finish_run(run_id, str(exc), failed=True)
    except Exception as exc:  # noqa: BLE001 — catch-all: run must settle
        logger.exception("chat_agent run %s crashed", run_id)
        _finish_run(run_id, f"The agent hit an error: {exc}"[:500], failed=True)
