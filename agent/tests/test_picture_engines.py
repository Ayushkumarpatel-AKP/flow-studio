"""Pictures when Google Flow's daily limit is reached.

1. Flow's quota is per image model, so the other Flow model is tried first (free).
2. After that, the user's own engines (OpenRouter, OpenAI) draw the picture.

No real keys or network: httpx runs on MockTransport.
"""
import base64
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from flowboard.services import engines, image_engines
from flowboard.worker import processor as proc

QUOTA = "RpcError: ogiZ0b failed: [8, None, [['x', ['PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED']]]]"
PROJECT = "8b62385c-4916-4abd-b01f-b28173d8eb04"
PNG = b"\x89PNG\r\n\x1a\nfakeimage"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "STORE_PATH", tmp_path / "engines.json")
    proc._FLOW_MODEL_UNAVAILABLE.clear()


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def connect(pid, image_model, **extra):
    models = {"text": "t", "fast": "t", "stt": None, "image": image_model, "tts": None, "vision": None}
    data = engines._load()
    data.setdefault("keys", {})[pid] = "k_" + pid
    data.setdefault("state", {})[pid] = {"status": "ok", "models": models, **extra}
    engines._save(data)


# ── Flow: try the other image model when one's daily quota is spent ──────────

@pytest.mark.asyncio
async def test_flow_quota_on_one_model_switches_to_the_other():
    with patch("flowboard.worker.processor.get_flow_sdk") as m:
        m.return_value.gen_image = AsyncMock(side_effect=[
            {"error": QUOTA},                                   # Nano Banana Pro is out for today
            {"media_ids": ["m"], "media_entries": []},          # Nano Banana 2 still has room
        ])
        resp, err = await proc._handle_gen_image({"prompt": "a cat", "project_id": PROJECT})
    assert err is None
    used = [c.kwargs["image_model"] for c in m.return_value.gen_image.call_args_list]
    assert used == ["NANO_BANANA_PRO", "NANO_BANANA_2"]          # stops at the first model that works


@pytest.mark.asyncio
async def test_flow_reports_the_quota_error_when_every_model_is_spent():
    with patch("flowboard.worker.processor.get_flow_sdk") as m:
        m.return_value.gen_image = AsyncMock(return_value={"error": QUOTA})
        _resp, err = await proc._handle_gen_image({"prompt": "a cat", "project_id": PROJECT})
    assert "PER_MODEL_DAILY_QUOTA" in err
    assert m.return_value.gen_image.await_count == 3             # all three Flow models were tried, no endless loop


@pytest.mark.asyncio
async def test_other_flow_errors_are_not_retried_on_another_model():
    with patch("flowboard.worker.processor.get_flow_sdk") as m:
        m.return_value.gen_image = AsyncMock(return_value={"error": "CAPTCHA_FAILED: CAPTCHA_TIMEOUT"})
        _resp, err = await proc._handle_gen_image({"prompt": "a cat", "project_id": PROJECT})
    assert "CAPTCHA" in err and m.return_value.gen_image.await_count == 1


# ── choosing a picture model from a live catalogue ───────────────────────────

def test_pick_image_model_prefers_known_families_and_small_variants():
    ids = ["sourceful/riverflow-v2.6-pro", "black-forest-labs/flux-3-image", "black-forest-labs/flux-3-image-klein",
           "google/gemini-nano-banana-2.1", "openai/gpt-image-2.5-flare"]
    assert engines.pick_image_model(ids) == "google/gemini-nano-banana-2.1"
    assert engines.pick_image_model(["black-forest-labs/flux-3-image", "black-forest-labs/flux-3-image-klein"]) == "black-forest-labs/flux-3-image-klein"
    assert engines.pick_image_model(["acme/mystery"]) == "acme/mystery"
    assert engines.pick_image_model([]) is None


@pytest.mark.asyncio
async def test_openrouter_probe_reads_its_picture_catalogue():
    def handler(req):
        if req.url.path == "/api/v1/images/models":
            return httpx.Response(200, json={"data": [{"id": "google/gemini-nano-banana-2.1"}, {"id": "black-forest-labs/flux-3-image"}]})
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "meta-llama/llama-3.3-70b-instruct:free"}]})
        return httpx.Response(200, json={"choices": []})
    r = await engines.probe("openrouter", "sk-or-test", client=client(handler))
    assert r["status"] == "ok"
    assert r["models"]["image"] == "google/gemini-nano-banana-2.1"
    assert r["image_ids"] == ["google/gemini-nano-banana-2.1", "black-forest-labs/flux-3-image"]
    assert engines.job_compat(r["models"])["image"]["ok"]


# ── drawing on the user's engine ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openrouter_draws_with_the_images_api_and_aspect_ratio():
    connect("openrouter", "google/gemini-nano-banana-2.1")
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode(), "media_type": "image/png"}]})
    img, mime, pid, model = await image_engines.generate("a stickman waving", "9:16", client=client(handler))
    assert (img, mime, pid, model) == (PNG, "image/png", "openrouter", "google/gemini-nano-banana-2.1")
    assert seen["url"] == "https://openrouter.ai/api/v1/images" and seen["auth"] == "Bearer k_openrouter"
    assert seen["body"] == {"model": "google/gemini-nano-banana-2.1", "prompt": "a stickman waving", "aspect_ratio": "9:16"}


@pytest.mark.asyncio
async def test_openai_picture_request_uses_the_right_size_per_model_family():
    connect("openai", "gpt-image-1")
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    await image_engines.generate("x y z", "16:9", client=client(handler))
    assert bodies[0]["size"] == "1536x1024" and "response_format" not in bodies[0]
    connect("openai", "dall-e-3")
    await image_engines.generate("x y z", "9:16", client=client(handler))
    assert bodies[1]["size"] == "1024x1792" and bodies[1]["response_format"] == "b64_json"


@pytest.mark.asyncio
async def test_a_failing_engine_falls_through_to_the_next_one():
    connect("openrouter", "m-or")
    connect("openai", "gpt-image-1")

    def handler(req):
        if "openrouter" in req.url.host:
            return httpx.Response(402, json={"error": {"message": "insufficient credits"}})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
    _img, _mime, pid, _model = await image_engines.generate("draw this", "16:9", client=client(handler))
    assert pid == "openai"


@pytest.mark.asyncio
async def test_clear_errors_when_nothing_is_connected_or_everything_fails():
    with pytest.raises(image_engines.ImageEngineError, match="OpenRouter or OpenAI"):
        await image_engines.generate("anything at all", "16:9")
    connect("openrouter", "m-or")
    with pytest.raises(image_engines.ImageEngineError, match="insufficient credits"):
        await image_engines.generate("anything at all", "16:9", client=client(
            lambda req: httpx.Response(402, json={"error": {"message": "insufficient credits"}})))


def test_status_line_says_flow_first_then_the_engine():
    connect("openrouter", "m-or")
    s = engines.job_status(engines._load(), "image")
    assert s["state"] == "ok" and "Flow draws first" in s["message"] and "OpenRouter" in s["message"]
    assert engines.job_status({}, "image")["state"] == "none"


def test_image_route_saves_the_picture_and_returns_a_media_id(client_app, monkeypatch):
    async def fake_generate(prompt, fmt="16:9", client=None):
        return PNG, "image/png", "openrouter", "m-or"
    monkeypatch.setattr(image_engines, "generate", fake_generate)
    r = client_app.post("/api/studio/image", json={"prompt": "a stickman", "format": "16:9"})
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "openrouter" and len(body["media_id"]) == 36
    assert client_app.get(f"/media/{body['media_id']}").content == PNG     # served like any other media


def test_image_route_reports_a_helpful_error_without_an_engine(client_app):
    r = client_app.post("/api/studio/image", json={"prompt": "a stickman"})
    assert r.status_code == 502 and "OpenRouter or OpenAI" in r.json()["detail"]


@pytest.fixture
def client_app(client):
    return client


# ── OpenRouter free tier cannot draw ─────────────────────────────────────────

def _openrouter_handler(free_tier: bool):
    def handler(req):
        if req.url.path == "/api/v1/images/models":
            return httpx.Response(200, json={"data": [{"id": "google/gemini-nano-banana-2.1"}]})
        if req.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"is_free_tier": free_tier}})
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "meta-llama/llama-3.3-70b-instruct:free"}]})
        return httpx.Response(200, json={"choices": []})
    return handler


@pytest.mark.asyncio
async def test_free_tier_openrouter_is_not_offered_for_pictures():
    r = await engines.probe("openrouter", "sk-or-test", client=client(_openrouter_handler(True)))
    assert r["status"] == "ok" and r["models"]["image"] is None                 # text still works
    assert "purchased credits" in r["image_blocked"] and r["tier"]["free"] is True
    assert not engines.job_compat(r["models"])["image"]["ok"]
    engines._save({"keys": {"openrouter": "sk-or-test"}, "state": {"openrouter": {"status": "ok", **{k: r[k] for k in ("models", "image_blocked")}}}, "assignments": {}})
    msg = engines.job_status(engines._load(), "image")["message"]
    assert msg.startswith("No connected engine") and "free tier" in msg and "OpenAI" in msg


@pytest.mark.asyncio
async def test_funded_openrouter_account_can_draw():
    r = await engines.probe("openrouter", "sk-or-test", client=client(_openrouter_handler(False)))
    assert r["models"]["image"] == "google/gemini-nano-banana-2.1" and r["image_blocked"] is None



# ── the "[5]" bug: a model Flow does not have must not hide the real (quota) reason ──

NOT_FOUND = "RpcError: ogiZ0b failed: [5]"


@pytest.mark.asyncio
async def test_not_found_from_a_fallback_model_does_not_hide_the_quota_error():
    with patch("flowboard.worker.processor.get_flow_sdk") as m:
        m.return_value.gen_image = AsyncMock(side_effect=[
            {"error": QUOTA},          # Pro is out for today
            {"error": NOT_FOUND},      # this account has no "Nano Banana 2"
            {"error": NOT_FOUND},      # nor the Lite one
        ])
        _resp, err = await proc._handle_gen_image({"prompt": "a cat", "project_id": PROJECT})
    assert "PER_MODEL_DAILY_QUOTA" in err and "[5]" not in err      # the person sees the real reason


@pytest.mark.asyncio
async def test_a_model_flow_does_not_have_is_skipped_next_time_and_the_lite_model_still_works():
    with patch("flowboard.worker.processor.get_flow_sdk") as m:
        m.return_value.gen_image = AsyncMock(side_effect=[
            {"error": QUOTA}, {"error": NOT_FOUND}, {"media_ids": ["m"], "media_entries": []},   # Pro, Banana 2, Lite
            {"error": QUOTA}, {"media_ids": ["m2"], "media_entries": []},                         # next picture: Banana 2 is skipped
        ])
        _r, err1 = await proc._handle_gen_image({"prompt": "a cat", "project_id": PROJECT})
        _r, err2 = await proc._handle_gen_image({"prompt": "a dog", "project_id": PROJECT})
    assert err1 is None and err2 is None
    used = [c.kwargs["image_model"] for c in m.return_value.gen_image.call_args_list]
    assert used == ["NANO_BANANA_PRO", "NANO_BANANA_2", "NANO_BANANA_2_LITE", "NANO_BANANA_PRO", "NANO_BANANA_2_LITE"]


@pytest.mark.asyncio
async def test_not_found_on_the_first_try_is_not_blamed_on_the_model():
    """[5] with no quota error first points at the project/request, so no model gets blacklisted."""
    with patch("flowboard.worker.processor.get_flow_sdk") as m:
        m.return_value.gen_image = AsyncMock(return_value={"error": NOT_FOUND})
        _r, err = await proc._handle_gen_image({"prompt": "a cat", "project_id": PROJECT})
    assert "[5]" in err and m.return_value.gen_image.await_count == 1
    assert not proc._FLOW_MODEL_UNAVAILABLE


# ── a picture drawn by the user's engine can be turned into a video ──────────────

@pytest.mark.asyncio
async def test_a_picture_with_no_download_url_is_found_in_the_local_cache(tmp_path):
    """Pictures from OpenRouter/OpenAI are saved straight to the cache (no URL). Rendering used to
    ask for a download, found none, and failed with '404: media not available'."""
    from flowboard.routes import studio
    from flowboard.services import media as media_service
    mid = "6f1c2a3e-1111-4222-8333-444455556666"
    assert media_service.ingest_inline_bytes(mid, PNG, kind="image", mime="image/png")
    path = await studio._local_file(mid)
    assert path.read_bytes() == PNG
    with pytest.raises(Exception, match="not available"):
        await studio._local_file("00000000-1111-4222-8333-000000000000")   # truly unknown media still 404s
