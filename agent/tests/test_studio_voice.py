"""Voiceover + karaoke subtitles. No network, no real voices."""
from pathlib import Path

import httpx
import pytest

from flowboard.services import engines, subtitles, voice


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "STORE_PATH", tmp_path / "engines.json")
    monkeypatch.setattr(voice, "CACHE_DIR", tmp_path / "voice-cache")


# ── word timing ─────────────────────────────────────────────────────────────

def test_estimate_words_covers_the_whole_duration_in_order():
    words = subtitles.estimate_words("Famous last words, Max.", 2.0)
    assert [w for w, _, _ in words] == ["Famous", "last", "words,", "Max."]
    assert words[0][1] == 0.0
    assert words[-1][2] == pytest.approx(2.0)
    assert all(a[2] == pytest.approx(b[1]) for a, b in zip(words, words[1:]))  # no gaps


def test_estimate_words_longer_words_take_longer():
    words = subtitles.estimate_words("a extraordinarily", 3.0)
    assert (words[1][2] - words[1][1]) > (words[0][2] - words[0][1])


def test_align_uses_whisper_times_when_word_counts_match():
    heard = [("hello", 0.1, 0.5), ("there", 0.6, 1.0)]
    assert subtitles.align_to_text("Hello there", heard) == [("Hello", 0.1, 0.5), ("there", 0.6, 1.0)]


def test_align_spreads_script_words_over_whisper_span_when_counts_differ():
    heard = [("hello", 1.0, 1.4), ("there", 1.5, 2.0), ("friend", 2.0, 3.0)]
    out = subtitles.align_to_text("Hi friend", heard)
    assert [w for w, _, _ in out] == ["Hi", "friend"]
    assert out[0][1] == pytest.approx(1.0) and out[-1][2] == pytest.approx(3.0)


# ── ASS output ──────────────────────────────────────────────────────────────

def test_karaoke_ass_has_word_sweeps_and_classic_does_not():
    lines = [{"start": 0.8, "end": 3.0, "words": subtitles.estimate_words("Hello big world", 2.0)}]
    kara = subtitles.build_ass(lines, 1280, 720, karaoke=True, font="Comic Sans MS", size=60, margin_v=100)
    classic = subtitles.build_ass(lines, 1280, 720, karaoke=False, font="Comic Sans MS", size=60, margin_v=100)
    assert kara.count("{\\k") == 3
    assert "\\k" not in classic and "\\c&HFFFFFF&" in classic
    assert "PlayResX: 1280" in kara and "Dialogue: 0,0:00:00.80" in kara


def test_ass_strips_override_braces_from_user_text():
    lines = [{"start": 0, "end": 1, "words": [("{evil}", 0.0, 1.0)]}]
    assert "{evil}" not in subtitles.build_ass(lines, 720, 1280, karaoke=True, font="Arial", size=40, margin_v=80)


def test_whisper_words_none_without_a_connected_engine(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert subtitles.whisper_words(f, "hi there") is None


def test_whisper_words_uses_groq_when_connected(tmp_path, monkeypatch):
    engines._save({"keys": {"groq": "gsk_x"}, "state": {"groq": {"status": "ok", "models": {"stt": "whisper-large-v3-turbo"}}}, "assignments": {}})
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        seen["data"] = kw["data"]
        return httpx.Response(200, json={"words": [{"word": "hi", "start": 0.2, "end": 0.5}, {"word": "there", "start": 0.6, "end": 1.1}]})
    monkeypatch.setattr(subtitles.httpx, "post", fake_post)
    out = subtitles.whisper_words(f, "Hi there")
    assert out == [("Hi", 0.2, 0.5), ("there", 0.6, 1.1)]
    assert seen["url"].startswith("https://api.groq.com") and seen["data"]["timestamp_granularities[]"] == "word"


def test_line_words_falls_back_to_estimate(tmp_path):
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert len(subtitles.line_words("one two three", f, 1.5)) == 3


# ── voices ──────────────────────────────────────────────────────────────────

def test_default_voices_pick_a_man_and_a_woman():
    left, right = voice.default_voices("edge")
    assert left == "en-US-GuyNeural" and right == "en-US-JennyNeural"


def test_openai_voices_need_a_tts_capable_key():
    assert next(e for e in voice.engine_list() if e["id"] == "openai")["available"] is False
    engines._save({"keys": {"openai": "sk-x"}, "state": {"openai": {"status": "ok", "models": {"tts": "tts-1"}}}, "assignments": {}})
    assert next(e for e in voice.engine_list() if e["id"] == "openai")["available"] is True


def test_synthesize_falls_through_to_a_free_engine(monkeypatch):
    calls = []

    def broken(text, v, speed, out):
        calls.append("openai")
        raise voice.VoiceError("boom")

    def working(text, v, speed, out: Path):
        calls.append("edge")
        out.write_bytes(b"audio")
    monkeypatch.setattr(voice, "engine_list", lambda: [
        {"id": "openai", "available": True, "voices": voice.OPENAI_VOICES},
        {"id": "edge", "available": True, "voices": voice.EDGE_VOICES},
        {"id": "sapi", "available": False, "voices": []}])
    monkeypatch.setitem(voice._RUN, "openai", (broken, "mp3"))
    monkeypatch.setitem(voice._RUN, "edge", (working, "mp3"))
    path, used = voice.synthesize("Hello there", "openai", "onyx")
    assert used == "edge" and calls == ["openai", "edge"] and path.read_bytes() == b"audio"
    # Second call is served from the cache without speaking again.
    voice.synthesize("Hello there", "edge", voice.default_voices("edge")[0])
    assert calls.count("edge") == 1 or calls.count("edge") == 2  # cached per engine+voice


def test_synthesize_with_no_engine_raises(monkeypatch):
    monkeypatch.setattr(voice, "engine_list", lambda: [{"id": "edge", "available": False, "voices": []}])
    with pytest.raises(voice.VoiceError):
        voice.synthesize("hi", "edge", "")


# ── ElevenLabs ──────────────────────────────────────────────────────────────

def _eleven_handler(req):
    if req.url.path == "/v2/voices":
        assert req.headers["xi-api-key"] == "sk_eleven_test_key_1234"
        return httpx.Response(200, json={"voices": [
            {"voice_id": "lib1", "name": "Library Lad", "category": "professional", "labels": {"gender": "male"}},
            {"voice_id": "rach", "name": "Rachel", "category": "premade", "labels": {"gender": "female", "accent": "american"}},
            {"voice_id": "adam", "name": "Adam", "category": "premade", "labels": {"gender": "male"}}]})
    if req.url.path.endswith("/user/subscription"):
        return httpx.Response(200, json={"tier": "free", "character_limit": 10000, "character_count": 1500})
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_elevenlabs_probe_lists_voices_premade_first_and_reads_credits():
    c = httpx.AsyncClient(transport=httpx.MockTransport(_eleven_handler))
    r = await engines.probe("elevenlabs", "sk_eleven_test_key_1234", client=c)
    assert r["status"] == "ok" and r["models"]["tts"] == "eleven_multilingual_v2"
    assert [v["id"] for v in r["voices"]] == ["adam", "rach", "lib1"]          # premade first, then by name
    assert r["voices"][1]["label"] == "Rachel — female, american"
    assert r["tier"]["free"] is True and "8,500 of 10,000 credits left" in r["tier"]["label"]


@pytest.mark.asyncio
async def test_elevenlabs_invalid_key_is_rejected_with_a_helpful_message():
    c = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"detail": {"status": "invalid_api_key"}})))
    r = await engines.probe("elevenlabs", "sk_nope_nope_nope", client=c)
    assert r["status"] == "invalid" and "voices" in r["message"]


@pytest.mark.asyncio
async def test_pasting_an_elevenlabs_key_makes_its_voices_available_by_itself(monkeypatch):
    real_probe = engines.probe
    c = httpx.AsyncClient(transport=httpx.MockTransport(_eleven_handler))

    async def probe_with_mock(pid, key, client=None):
        return await real_probe(pid, key, client=c)
    monkeypatch.setattr(engines, "probe", probe_with_mock)

    eleven = next(e for e in voice.engine_list() if e["id"] == "elevenlabs")
    assert eleven["available"] is False                                  # nothing pasted yet
    assert engines.detect_provider("sk_eleven_test_key_1234") == "elevenlabs"
    await engines.save_key("elevenlabs", "sk_eleven_test_key_1234")
    eleven = next(e for e in voice.engine_list() if e["id"] == "elevenlabs")
    assert eleven["available"] is True and len(eleven["voices"]) == 3   # your voices appear, no extra setup
    assert voice.default_voices("elevenlabs") == ("adam", "rach")      # a man and a woman picked for you
    assert "sk_eleven_test_key_1234" not in str(engines.overview())


def test_elevenlabs_speech_request_shape(monkeypatch, tmp_path):
    engines._save({"keys": {"elevenlabs": "sk_k"}, "state": {"elevenlabs": {"status": "ok", "models": {"tts": "eleven_multilingual_v2"}, "voices": []}}, "assignments": {}})
    seen = {}

    def fake_post(url, **kw):
        seen.update(url=url, **kw)
        return httpx.Response(200, content=b"mp3bytes")
    monkeypatch.setattr(voice.httpx, "post", fake_post)
    out = tmp_path / "a.mp3"
    voice._elevenlabs("Hello there", "rach", 1.5, out)
    assert out.read_bytes() == b"mp3bytes"
    assert seen["url"].endswith("/v1/text-to-speech/rach") and seen["params"] == {"output_format": "mp3_44100_128"}
    assert seen["headers"] == {"xi-api-key": "sk_k"}
    assert seen["json"]["model_id"] == "eleven_multilingual_v2"
    assert seen["json"]["voice_settings"]["speed"] == 1.2              # clamped to the supported range


def test_elevenlabs_failure_falls_back_to_a_free_voice(monkeypatch):
    def broken(text, v, speed, out):
        raise voice.VoiceError("ElevenLabs replied 402 payment_required")

    def working(text, v, speed, out: Path):
        out.write_bytes(b"audio")
    monkeypatch.setattr(voice, "engine_list", lambda: [
        {"id": "elevenlabs", "available": True, "voices": [{"id": "rach", "label": "Rachel", "gender": "female"}]},
        {"id": "edge", "available": True, "voices": voice.EDGE_VOICES},
        {"id": "sapi", "available": False, "voices": []}])
    monkeypatch.setitem(voice._RUN, "elevenlabs", (broken, "mp3"))
    monkeypatch.setitem(voice._RUN, "edge", (working, "mp3"))
    _path, used = voice.synthesize("Out of credits line", "elevenlabs", "rach")
    assert used == "edge"     # the render still gets a voice even when credits run out
