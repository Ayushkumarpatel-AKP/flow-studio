"""Pictures from the user's own engines (OpenRouter, OpenAI).

Google Flow is the first choice for pictures. This is the backup for when Flow's
daily limit is reached, so a story can still be finished. Each call spends the
user's own credits with that service.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Optional

import httpx

from flowboard.services import connectors, engines

logger = logging.getLogger(__name__)

_OPENAI_SIZES = {"16:9": "1536x1024", "9:16": "1024x1536", "1:1": "1024x1024"}
_DALLE_SIZES = {"16:9": "1792x1024", "9:16": "1024x1792", "1:1": "1024x1024"}


class ImageEngineError(Exception):
    pass


def _error_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        err = j.get("error") if isinstance(j, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else err) or ""
    except (ValueError, AttributeError):
        msg = ""
    return f"HTTP {r.status_code} {str(msg)[:160]}".strip()


def _decode(r: httpx.Response) -> tuple[bytes, str]:
    try:
        item = r.json()["data"][0]
        return base64.b64decode(item["b64_json"]), item.get("media_type") or "image/png"
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ImageEngineError(f"unexpected reply ({exc.__class__.__name__})")


async def _openrouter(c: httpx.AsyncClient, key: str, model: str, prompt: str, fmt: str) -> tuple[bytes, str]:
    r = await c.post(f"{engines.PROVIDERS['openrouter']['base_url']}/images",
                     headers={"Authorization": f"Bearer {key}"},
                     json={"model": model, "prompt": prompt, "aspect_ratio": fmt if fmt in _OPENAI_SIZES else "16:9"})
    if r.status_code != 200:
        raise ImageEngineError(_error_text(r))
    return _decode(r)


async def _openai(c: httpx.AsyncClient, key: str, model: str, prompt: str, fmt: str) -> tuple[bytes, str]:
    dalle = model.startswith("dall-e")
    body: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1,
                            "size": (_DALLE_SIZES if dalle else _OPENAI_SIZES).get(fmt, "1024x1024")}
    if dalle:
        body["response_format"] = "b64_json"   # gpt-image models always return base64
    r = await c.post(f"{engines.PROVIDERS['openai']['base_url']}/images/generations",
                     headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code != 200:
        raise ImageEngineError(_error_text(r))
    return _decode(r)


_RUN = {"openrouter": _openrouter, "openai": _openai}


async def generate(prompt: str, fmt: str = "16:9", client: Optional[httpx.AsyncClient] = None,
                   ref: Optional[bytes] = None) -> tuple[bytes, str, str, str]:
    """Draw ``prompt`` on the first working picture engine.

    Returns (image bytes, mime type, engine id, model). Raises ``ImageEngineError``
    when nothing is connected or every engine fails.
    """
    data = engines._load()
    # Free connectors first when a reference picture is given: only they can use it (Cloudflare FLUX.2),
    # and it spends no money. Paid engines below draw from the text alone.
    order = [] if (ref and connectors.has_picture_connector()) else engines._text_order("image", data)
    if not order and not connectors.has_picture_connector():
        raise ImageEngineError("No picture service is connected. Open Connect and turn on a picture service, or add an OpenRouter or OpenAI key in Engines.")
    own = client is None
    c = client or httpx.AsyncClient(timeout=180)
    errors: list[str] = []
    try:
        for pid in order:
            model = engines._model_for("image", pid, data)
            run = _RUN.get(pid)
            if not model or run is None:
                continue
            try:
                img, mime = await run(c, data["keys"][pid], model, prompt, fmt)
                return img, mime, pid, model
            except (ImageEngineError, httpx.HTTPError) as exc:
                errors.append(f"{engines.PROVIDERS[pid]['label']}: {exc}")
                logger.info("picture engine %s failed: %s", pid, exc)
        if connectors.has_picture_connector():
            try:
                img, mime, label = await connectors.draw_picture(prompt, fmt, client=c, ref=ref)
                return img, mime, label, "connector"
            except connectors.ConnectorError as exc:
                errors.append(str(exc))
        raise ImageEngineError("; ".join(errors) or "no usable picture engine")
    finally:
        if own:
            await c.aclose()
