"""Bring-your-own-key engines: detection, compatibility, probing and fallback.

No real keys or network: httpx is driven through MockTransport.
"""
import httpx
import pytest

from flowboard.services import engines


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "STORE_PATH", tmp_path / "engines.json")


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def models_response(ids):
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]})


def test_detect_provider_by_prefix():
    assert engines.detect_provider("gsk_abc123") == "groq"
    assert engines.detect_provider("nvapi-xyz") == "nvidia"
    assert engines.detect_provider("sk-or-v1-abc") == "openrouter"  # longer prefix beats sk-
    assert engines.detect_provider("sk-proj-abc") == "openai"
    assert engines.detect_provider("AIzaSyABC") == "gemini"
    assert engines.detect_provider("hello") is None


def test_mask_never_reveals_middle():
    assert engines.mask("gsk_1234567890abcdef") == "gsk_…cdef"
    assert engines.mask("short") == "••••"


def test_analyse_models_picks_preferred_and_reports_jobs():
    ids = ["whisper-large-v3-turbo", "llama-3.1-8b-instant", "llama-3.3-70b-versatile"]
    m = engines.analyse_models("groq", ids)
    assert m["text"] == "llama-3.3-70b-versatile"
    assert m["fast"] == "llama-3.1-8b-instant"
    assert m["stt"] == "whisper-large-v3-turbo"
    assert m["image"] is None
    compat = engines.job_compat(m)
    assert compat["script"]["ok"] and compat["script"]["wired"]
    assert compat["subtitles"]["ok"] and compat["subtitles"]["wired"]
    assert not compat["image"]["wired"]  # pictures still come from Flow
    assert not compat["image"]["ok"]


def test_analyse_models_strips_gemini_prefix_and_falls_back():
    m = engines.analyse_models("gemini", ["models/gemini-2.0-flash", "models/embedding-001"])
    assert m["text"] == "gemini-2.0-flash"
    # An unknown but chat-like model is still usable when no preferred one exists.
    m2 = engines.analyse_models("nvidia", ["acme/llama-9-instruct", "nvidia/embed-qa"])
    assert m2["text"] == "acme/llama-9-instruct"


def test_tier_guess_groq_uses_token_limit():
    assert engines.tier_guess("groq", {"tokens": 6000})["free"] is True
    assert engines.tier_guess("groq", {"tokens": 250000})["free"] is False
    assert engines.tier_guess("openai", {})["free"] is False
    assert engines.tier_guess("groq", {"tokens": 6000})["estimate"] is True


@pytest.mark.asyncio
async def test_probe_ok_reads_models_and_limits():
    def handler(req):
        if req.url.path.endswith("/models"):
            return models_response(["llama-3.3-70b-versatile", "whisper-large-v3"])
        return httpx.Response(200, json={}, headers={"x-ratelimit-limit-tokens": "6000", "x-ratelimit-limit-requests": "1000"})
    r = await engines.probe("groq", "gsk_test", client=client(handler))
    assert r["status"] == "ok"
    assert r["models"]["text"] == "llama-3.3-70b-versatile"
    assert r["limits"]["tokens"] == 6000
    assert r["tier"]["free"] is True


@pytest.mark.asyncio
async def test_probe_invalid_key_even_when_model_list_is_public():
    def handler(req):
        if req.url.path.endswith("/models"):
            return models_response(["meta/llama-3.3-70b-instruct"])  # NVIDIA lists models without auth
        return httpx.Response(401, json={"error": {"message": "bad key"}})
    r = await engines.probe("nvidia", "nvapi-bad", client=client(handler))
    assert r["status"] == "invalid"


@pytest.mark.asyncio
async def test_probe_rate_limited_counts_as_valid():
    def handler(req):
        if req.url.path.endswith("/models"):
            return models_response(["gpt-4o-mini"])
        return httpx.Response(429, json={})
    r = await engines.probe("openai", "sk-test", client=client(handler))
    assert r["status"] == "rate_limited"


@pytest.mark.asyncio
async def test_save_key_stores_only_working_keys(monkeypatch):
    async def fake_probe(pid, key, client=None):
        return {"status": "invalid", "message": "no"} if key == "gsk_bad" else {
            "status": "ok", "message": "ok", "models": engines.analyse_models("groq", ["llama-3.3-70b-versatile"]),
            "limits": {}, "tier": {}, "checked_at": 1}
    monkeypatch.setattr(engines, "probe", fake_probe)
    assert (await engines.save_key("groq", "gsk_bad"))["status"] == "invalid"
    assert engines.get_key("groq") is None
    assert (await engines.save_key("groq", "gsk_good_key_123456"))["status"] == "ok"
    assert engines.get_key("groq") == "gsk_good_key_123456"
    view = next(p for p in engines.overview()["providers"] if p["id"] == "groq")
    assert view["configured"] and view["key_hint"] == "gsk_…3456"
    assert "gsk_good_key_123456" not in str(engines.overview())  # the key itself is never returned
    engines.remove_key("groq")
    assert engines.get_key("groq") is None


@pytest.mark.asyncio
async def test_chat_falls_through_to_next_engine(monkeypatch):
    models = engines.analyse_models("groq", ["llama-3.3-70b-versatile"])
    engines._save({"keys": {"groq": "gsk_a", "gemini": "AIza_b"},
                   "state": {"groq": {"status": "ok", "models": models},
                             "gemini": {"status": "ok", "models": engines.analyse_models("gemini", ["gemini-2.0-flash"])}},
                   "assignments": {}})
    calls = []

    def handler(req):
        calls.append(req.url.host)
        if "groq" in req.url.host:
            return httpx.Response(500)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello from gemini"}}]})
    out = await engines.chat("script", "hi", client=client(handler))
    assert out == "hello from gemini"
    assert calls[0].endswith("groq.com")  # best engine tried first


@pytest.mark.asyncio
async def test_chat_without_engines_raises_so_callers_can_fall_back():
    with pytest.raises(engines.EngineError):
        await engines.chat("script", "hi")


def test_recommendation_tips_follow_what_is_configured():
    empty = engines.recommend({"keys": {}, "state": {}})
    assert any("Groq" in t for t in empty["tips"])
    data = {"keys": {"groq": "k"}, "state": {"groq": {"status": "ok", "models": engines.analyse_models("groq", ["llama-3.3-70b-versatile", "whisper-large-v3"])}}}
    rec = engines.recommend(data)
    assert rec["picks"]["script"] == "groq"
    assert rec["picks"]["subtitles"] == "groq"
    assert rec["picks"]["image"] is None


@pytest.mark.asyncio
async def test_probe_treats_gemini_style_400_as_invalid_key():
    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(400, json=[{"error": {"message": "API key not valid. Please pass a valid API key."}}])
        return httpx.Response(400, json=[{"error": {"message": "API key not valid. Please pass a valid API key."}}])
    r = await engines.probe("gemini", "AIza_bad", client=client(handler))
    assert r["status"] == "invalid"


def _save_two_engines():
    groq = engines.analyse_models("groq", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"])  # no vision model
    gem = engines.analyse_models("gemini", ["models/gemini-2.0-flash"])
    engines._save({"keys": {"groq": "gsk_a", "gemini": "AIza_b"},
                   "state": {"groq": {"status": "ok", "models": groq}, "gemini": {"status": "ok", "models": gem}},
                   "assignments": {}})


def test_vision_order_skips_engines_that_cannot_see_pictures():
    _save_two_engines()
    data = engines._load()
    assert engines._text_order("vision", data) == ["gemini"]
    assert engines._text_order("autoprompt", data) == ["groq", "gemini"]  # text jobs keep best-first order


def test_canvas_jobs_are_wired_and_mapped_from_old_feature_names():
    assert engines.FEATURE_JOBS == {"auto_prompt": "autoprompt", "planner": "planner", "vision": "vision", "chat_agent": "chat"}
    for j in ("autoprompt", "planner", "vision", "chat"):
        assert engines.JOBS[j]["wired"]


@pytest.mark.asyncio
async def test_vision_chat_sends_the_picture_as_a_data_url(tmp_path):
    _save_two_engines()
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG fake")
    seen = {}

    def handler(req):
        import json as _json
        seen["body"] = _json.loads(req.content)
        seen["host"] = req.url.host
        return httpx.Response(200, json={"choices": [{"message": {"content": "a stickman"}}]})
    out = await engines.chat("vision", "Describe this", images=[str(img)], client=client(handler))
    assert out == "a stickman" and seen["host"].endswith("googleapis.com")
    parts = seen["body"]["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "Describe this"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_run_llm_gives_engines_first_refusal(monkeypatch):
    from flowboard.services.llm import registry

    async def fake_chat(job, user, **kw):
        assert job == "autoprompt" and kw["images"] is None
        return "served by engines"
    monkeypatch.setattr(registry.engines, "chat", fake_chat)
    assert await registry.run_llm("auto_prompt", "expand this") == "served by engines"



# ── model + backup per job ──────────────────────────────────────────────────

def _two_with_model_lists():
    groq_ids = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "whisper-large-v3", "meta-llama/llama-guard-4-12b"]
    nv_ids = ["meta/llama-3.3-70b-instruct", "meta/llama-3.1-8b-instruct", "nvidia/embed-qa"]
    engines._save({
        "keys": {"groq": "gsk_a", "nvidia": "nvapi-b"},
        "state": {
            "groq": {"status": "ok", "models": engines.analyse_models("groq", groq_ids), "model_ids": groq_ids},
            "nvidia": {"status": "ok", "models": engines.analyse_models("nvidia", nv_ids), "model_ids": nv_ids},
        },
        "assignments": {}})


def test_available_models_filters_by_job_and_puts_recommended_first():
    _two_with_model_lists()
    data = engines._load()
    models = engines.available_models("groq", "script", data)
    ids = [m["id"] for m in models]
    assert ids[0] == "llama-3.3-70b-versatile" and models[0]["recommended"]
    assert "whisper-large-v3" not in ids and "meta-llama/llama-guard-4-12b" not in ids  # not chat models
    assert [m["id"] for m in engines.available_models("groq", "subtitles", data)] == ["whisper-large-v3"]
    assert engines.available_models("groq", "vision", data) == []


def test_assignment_model_backup_order_and_legacy_string_format():
    _two_with_model_lists()
    engines.set_assignment("script", "nvidia", "meta/llama-3.1-8b-instruct", "groq")
    data = engines._load()
    assert engines._text_order("script", data) == ["nvidia", "groq"]
    assert engines._model_for("script", "nvidia", data) == "meta/llama-3.1-8b-instruct"   # the user's pick
    assert engines._model_for("script", "groq", data) == "llama-3.3-70b-versatile"        # backup uses its default
    # Older settings files stored just the engine id.
    data["assignments"]["rewrite"] = "nvidia"
    assert engines.assigned(data, "rewrite") == {"engine": "nvidia", "model": None, "backup": None}
    assert engines._text_order("rewrite", data)[0] == "nvidia"


def test_set_assignment_rejects_bad_choices():
    _two_with_model_lists()
    with pytest.raises(engines.EngineError):
        engines.set_assignment("script", "openai")                       # no key
    with pytest.raises(engines.EngineError):
        engines.set_assignment("script", "groq", None, "groq")           # backup same as engine
    with pytest.raises(engines.EngineError):
        engines.set_assignment("script", "groq", "not-a-real-model")     # model the key cannot use
    with pytest.raises(engines.EngineError):
        engines.set_assignment("script", None, "llama-3.3-70b-versatile")  # model without engine
    engines.set_assignment("script", "groq", None, "nvidia")
    engines.set_assignment("script")                                     # back to Auto
    assert "script" not in engines._load()["assignments"]


def test_removing_a_key_clears_it_from_choices():
    _two_with_model_lists()
    engines.set_assignment("script", "nvidia", None, "groq")
    engines.remove_key("groq")
    a = engines.assigned(engines._load(), "script")
    assert a["engine"] == "nvidia" and a["backup"] is None
    engines.remove_key("nvidia")
    assert "script" not in engines._load()["assignments"]


def test_job_status_explains_what_runs_and_what_to_do():
    _two_with_model_lists()
    ok = engines.job_status(engines._load(), "script")
    assert ok["state"] == "ok" and ok["engine"] == "groq" and "Backup: NVIDIA" in ok["message"]
    none = engines.job_status(engines._load(), "vision")
    assert none["state"] == "none" and "Gemini" in none["message"]


def test_overview_exposes_per_job_config_with_options():
    _two_with_model_lists()
    engines.set_assignment("script", "nvidia", None, "groq")
    cfg = engines.overview()["config"]["script"]
    assert cfg["engine"] == "nvidia" and cfg["backup"] == "groq" and cfg["status"]["state"] == "ok"
    assert {o["id"] for o in cfg["options"]} == {"groq", "nvidia"}
    assert "gsk_a" not in str(engines.overview())
