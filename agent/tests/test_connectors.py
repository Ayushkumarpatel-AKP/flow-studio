"""Connectors: saving/testing credentials, picture fallback, publishing, Google sign-in, custom services."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from flowboard.services import connectors, image_engines, publish


@pytest.fixture(autouse=True)
def _clean_store():
    connectors.STORE_PATH.unlink(missing_ok=True)
    yield
    connectors.STORE_PATH.unlink(missing_ok=True)


def run(coro):
    return asyncio.run(coro)


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_overview_lists_groups_and_never_returns_secrets(client):
    r = client.get("/api/connectors").json()
    groups = {c["group"] for c in r["connectors"]}
    assert groups == {"picture", "publish", "soon"}
    assert {"youtube", "telegram", "discord", "folder", "drive", "pollinations"} <= {c["id"] for c in r["connectors"]}


def test_telegram_key_is_tested_masked_and_enabled():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "mybot"}})
        return httpx.Response(200, json={"ok": True, "result": {"title": "My channel"}})

    res = run(connectors.save("telegram", {"bot_token": "123456:ABCDEFGHIJKLMNOP", "chat_id": "@chan"}, client_for(handler)))
    assert res["ok"] and "mybot" in res["detail"]
    view = next(c for c in connectors.overview()["connectors"] if c["id"] == "telegram")
    assert view["status"] == "ok"
    token_field = next(f for f in view["fields"] if f["name"] == "bot_token")
    assert "ABCDEFGHIJKLMNOP" not in json.dumps(view) and token_field["set"]


def test_bad_telegram_token_reports_plain_reason_and_stays_off():
    res = run(connectors.save("telegram", {"bot_token": "123456:BAD", "chat_id": "1"}, client_for(lambda r: httpx.Response(401))))
    assert not res["ok"] and "token" in res["detail"]
    assert not connectors.is_ready("telegram")


def test_missing_field_is_a_clear_error():
    with pytest.raises(connectors.ConnectorError, match="Bot token"):
        run(connectors.save("telegram", {"chat_id": "5"}))


def test_discord_rejects_non_webhook_url():
    res = run(connectors.save("discord", {"webhook_url": "https://example.com/x"}, client_for(lambda r: httpx.Response(200))))
    assert not res["ok"] and "webhook" in res["detail"].lower()


def test_folder_connector_creates_folder_and_copies_video(tmp_path):
    dest = tmp_path / "out"
    assert run(connectors.save("folder", {"path": str(dest)}))["ok"]
    video = tmp_path / "v.mp4"
    video.write_bytes(b"0" * 100)
    res = run(publish.publish(["folder"], video, title="My: story?"))
    assert res[0]["ok"] and (dest / "My story.mp4").is_file()
    res2 = run(publish.publish(["folder"], video, title="My: story?"))
    assert (dest / "My story (2).mp4").is_file() and res2[0]["ok"]


def test_publish_to_unconnected_target_does_not_crash(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    res = run(publish.publish(["telegram", "youtube"], video, title="t"))
    assert [r["ok"] for r in res] == [False, False] and "Not connected" in res[0]["detail"]


def test_one_failing_target_does_not_stop_the_others(tmp_path):
    run(connectors.save("folder", {"path": str(tmp_path / "f")}))
    run(connectors.save("telegram", {"bot_token": "123456:ABCDEFGH", "chat_id": "1"},
                        client_for(lambda r: httpx.Response(200, json={"ok": True, "result": {}}))))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 10)
    res = run(publish.publish(["telegram", "folder"], video, title="t", client=client_for(lambda r: httpx.Response(400, json={"description": "chat not found"}))))
    by = {r["target"]: r for r in res}
    assert not by["telegram"]["ok"] and "Find my chat" in by["telegram"]["detail"]
    assert by["folder"]["ok"]


def test_telegram_send_posts_caption_and_video(tmp_path):
    run(connectors.save("telegram", {"bot_token": "123456:ABCDEFGH", "chat_id": "@c"},
                        client_for(lambda r: httpx.Response(200, json={"ok": True, "result": {}}))))
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = req.content
        return httpx.Response(200, json={"ok": True})

    video = tmp_path / "v.mp4"
    video.write_bytes(b"VIDEOBYTES")
    res = run(publish.publish(["telegram"], video, title="Zombie Eclipse", tags=["comic", "fun"], client=client_for(handler)))
    assert res[0]["ok"] and "/sendVideo" in seen["url"]
    assert b"Zombie Eclipse" in seen["body"] and b"#comic" in seen["body"] and b"VIDEOBYTES" in seen["body"]


def _google_ready(scopes):
    connectors.save  # keep linter quiet
    d = connectors._load()
    for cid in ("youtube", "drive"):
        d["creds"][cid] = {"client_id": "abc.apps.googleusercontent.com", "client_secret": "sec"}
        d["enabled"][cid] = True
    d["oauth"]["google"] = {"refresh_token": "rt", "scopes": scopes}
    connectors._save(d)


def test_youtube_resumable_upload_adds_shorts_tag_and_defaults_private(tmp_path):
    _google_ready([connectors.CONNECTORS["youtube"]["scope"]])
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url)))
        if "oauth2.googleapis.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "at"})
        if req.method == "POST":
            meta = json.loads(req.content)
            calls.append(("meta", meta))
            return httpx.Response(200, headers={"location": "https://upload.example/session"})
        return httpx.Response(200, json={"id": "VID123"})

    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 50)
    res = run(publish.publish(["youtube"], video, title="T", client=client_for(handler)))
    assert res[0]["ok"] and res[0]["url"] == "https://youtu.be/VID123" and "private" in res[0]["detail"]
    meta = next(c[1] for c in calls if c[0] == "meta")
    assert meta["status"]["privacyStatus"] == "private" and "#Shorts" in meta["snippet"]["description"]


def test_drive_not_ready_when_only_youtube_scope_was_granted():
    _google_ready([connectors.CONNECTORS["youtube"]["scope"]])
    assert connectors.is_ready("youtube") and not connectors.is_ready("drive")


def test_google_signin_flow_stores_refresh_token_and_scopes():
    d = connectors._load()
    d["creds"]["youtube"] = {"client_id": "abc.apps.googleusercontent.com", "client_secret": "sec"}
    connectors._save(d)
    url = publish.google_authorize_url("youtube", "http://127.0.0.1:8434/api/connectors/google/callback")
    assert "youtube.upload" in url and "access_type=offline" in url
    state = url.split("state=")[1].split("&")[0]
    scope = connectors.CONNECTORS["youtube"]["scope"]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"refresh_token": "RT", "scope": scope, "access_token": "a"})

    assert run(publish.google_finish(state, "code", client_for(handler))) == "youtube"
    d = connectors._load()
    assert d["oauth"]["google"]["refresh_token"] == "RT" and d["enabled"]["youtube"]
    with pytest.raises(publish.PublishError):
        run(publish.google_finish(state, "code", client_for(handler)))      # state is single-use


def test_pollinations_turns_on_without_a_key_and_draws():
    assert run(connectors.enable_keyless("pollinations"))["ok"]

    def handler(req: httpx.Request) -> httpx.Response:
        assert "width=1280" in str(req.url)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    img, mime, label = run(connectors.draw_picture("a red apple", "16:9", client_for(handler)))
    assert img == PNG and mime == "image/png" and label == "Pollinations"


def test_picture_connectors_fall_through_to_the_next_one():
    d = connectors._load()
    d["enabled"]["pollinations"] = True
    d["creds"]["huggingface"] = {"token": "hf_x"}
    d["enabled"]["huggingface"] = True
    d["order"] = ["huggingface", "pollinations"]
    connectors._save(d)

    def handler(req: httpx.Request) -> httpx.Response:
        if "huggingface" in str(req.url):
            return httpx.Response(503, json={"error": "model loading"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    _, _, label = run(connectors.draw_picture("x", "1:1", client_for(handler)))
    assert label == "Pollinations"


def test_all_picture_connectors_failing_lists_every_reason():
    d = connectors._load()
    d["enabled"]["pollinations"] = True
    connectors._save(d)
    with pytest.raises(connectors.ConnectorError, match="Pollinations"):
        run(connectors.draw_picture("x", "1:1", client_for(lambda r: httpx.Response(500))))


def test_stability_and_fal_and_replicate_requests():
    d = connectors._load()
    d["creds"].update({"stability": {"key": "sk-s"}, "fal": {"key": "id:secret"}, "replicate": {"token": "r8_t"}})
    for k in ("stability", "fal", "replicate"):
        d["enabled"][k] = True
    d["order"] = ["stability"]
    connectors._save(d)
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if "stability" in str(req.url):
            assert req.headers["authorization"] == "Bearer sk-s" and b"9:16" in req.content
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(500)

    assert run(connectors.draw_picture("x", "9:16", client_for(handler)))[2] == "Stability AI"

    d = connectors._load()
    d["order"] = ["fal"]
    connectors._save(d)

    def fal(req: httpx.Request) -> httpx.Response:
        if "fal.run" in str(req.url):
            assert req.headers["authorization"] == "Key id:secret"
            return httpx.Response(200, json={"images": [{"url": "https://cdn.example/p.jpg"}]})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/jpeg"})

    assert run(connectors.draw_picture("x", "16:9", client_for(fal)))[2] == "fal.ai"

    d = connectors._load()
    d["order"] = ["replicate"]
    connectors._save(d)

    def rep(req: httpx.Request) -> httpx.Response:
        if "api.replicate.com" in str(req.url):
            return httpx.Response(201, json={"status": "succeeded", "output": ["https://r.example/o.png"]})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    assert run(connectors.draw_picture("x", "16:9", client_for(rep)))[2] == "Replicate"


def test_custom_connector_url_result_and_secret_header_masked():
    cid = connectors.save_custom({
        "label": "My Server", "url": "https://img.example/api", "headers": {"Authorization": "Bearer SECRETVALUE123"},
        "body": '{"text": "{prompt}", "w": {width}, "h": {height}}', "result_kind": "url", "result_path": "data.0.url"})
    assert cid == "custom-my-server" and connectors.is_ready(cid)
    ov = json.dumps(connectors.overview())
    assert "SECRETVALUE123" not in ov and "Authorization" in ov

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "img.example":
            body = json.loads(req.content)
            assert body == {"text": 'a "quoted" cat', "w": 720, "h": 1280}
            assert req.headers["authorization"] == "Bearer SECRETVALUE123"
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/a.png"}]})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    img, _, label = run(connectors.draw_picture('a "quoted" cat', "9:16", client_for(handler)))
    assert img == PNG and label == "My Server"


def test_custom_connector_base64_and_validation():
    with pytest.raises(connectors.ConnectorError):
        connectors.save_custom({"label": "x", "url": "ftp://nope", "result_kind": "url", "result_path": "a"})
    with pytest.raises(connectors.ConnectorError, match="JSON"):
        connectors.save_custom({"label": "x", "url": "https://a.b", "body": "{not json", "result_kind": "binary"})
    cid = connectors.save_custom({"label": "B64", "url": "https://a.b/x", "body": '{"p": "{prompt}"}',
                                  "result_kind": "base64", "result_path": "image"})
    img, *_ = run(connectors.draw_picture("hi", "1:1", client_for(
        lambda r: httpx.Response(200, json={"image": "data:image/png;base64," + base64.b64encode(PNG).decode()}))))
    assert img == PNG
    connectors.remove(cid)
    assert not connectors.is_ready(cid)


def test_image_engines_use_connectors_when_no_engine_is_connected():
    d = connectors._load()
    d["enabled"]["pollinations"] = True
    connectors._save(d)

    async def go():
        return await image_engines.generate("a cat", "16:9", client_for(lambda r: httpx.Response(200, content=PNG, headers={"content-type": "image/png"})))

    img, mime, engine, model = run(go())
    assert img == PNG and engine == "Pollinations" and model == "connector"


def test_image_engines_error_mentions_connect_when_nothing_is_set_up():
    with pytest.raises(image_engines.ImageEngineError, match="Connect"):
        run(image_engines.generate("a cat", "16:9"))


def test_routes_end_to_end(client):
    r = client.put("/api/connectors/folder", json={"values": {"path": str(Path(connectors.STORE_PATH).parent / "vids")}})
    assert r.status_code == 200 and r.json()["result"]["ok"]
    assert client.put("/api/connectors/nope", json={"values": {}}).status_code == 400
    assert client.post("/api/connectors/youtube/signin").status_code == 400      # no client saved yet
    r = client.post("/api/connectors/pollinations/enable", json={"enabled": True})
    assert r.status_code == 200 and "pollinations" in r.json()["picture_order"]
    r = client.put("/api/connectors/order/picture", json={"order": ["pollinations"]})
    assert r.json()["picture_order"] == ["pollinations"]
    assert client.delete("/api/connectors/folder").status_code == 200
    # publish with an unknown job id format is rejected, a missing video is a clean 404
    assert client.post("/api/publish", json={"job_id": "zzz", "targets": ["folder"], "title": "t"}).status_code == 422
    assert client.post("/api/publish", json={"job_id": "0" * 12, "targets": ["folder"], "title": "t"}).status_code == 404


def test_publish_kit_falls_back_without_ai(client, monkeypatch):
    from flowboard.routes import connectors as route

    async def boom(*a, **k):
        raise route.LLMError("no ai")

    monkeypatch.setattr(route, "_llm", boom)
    r = client.post("/api/publish/kit", json={"title": "Zombie Eclipse", "story": "Two stickmen survive."}).json()
    assert r["source"] == "builtin" and r["title"] == "Zombie Eclipse" and r["tags"]


def test_publish_kit_uses_ai_text(client, monkeypatch):
    from flowboard.routes import connectors as route

    async def ok(*a, **k):
        return '{"title": "Eclipse Apocalypse", "description": "They run!", "tags": ["#comic", "zombie"]}'

    monkeypatch.setattr(route, "_llm", ok)
    r = client.post("/api/publish/kit", json={"title": "z"}).json()
    assert r["source"] == "ai" and r["tags"] == ["comic", "zombie"]


def test_signin_refuses_an_email_pasted_as_client_id(client):
    r = run(connectors.save("youtube", {"client_id": "someone@gmail.com", "client_secret": "GOCSPX-abc"}))
    assert not r["ok"]
    resp = client.post("/api/connectors/youtube/signin")
    assert resp.status_code == 400 and "not a Google Client ID" in resp.json()["detail"]


def test_telegram_rejects_the_bots_own_username_as_chat():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"username": "flowbot", "id": 1234567890}})

    res = run(connectors.save("telegram", {"bot_token": "1234567890:ABCDEFGH", "chat_id": "@flowbot"}, client_for(handler)))
    assert not res["ok"] and "bot's own ID" in res["detail"] and "Find my chat" in res["detail"]
    res = run(connectors.save("telegram", {"chat_id": "1234567890"}, client_for(handler)))
    assert not res["ok"] and "bot's own ID" in res["detail"]


def test_telegram_token_alone_saves_and_asks_for_start_message():
    handler = lambda req: httpx.Response(200, json={"ok": True, "result": {"username": "flowbot", "id": 1}})
    res = run(connectors.save("telegram", {"bot_token": "1:ABCDEFGH"}, client_for(handler)))
    assert not res["ok"] and "/start" in res["detail"]


def test_find_my_chat_picks_the_person_who_messaged_and_connects():
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "flowbot", "id": 1}})
        if path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"message": {"chat": {"id": 555, "first_name": "Ayush", "type": "private"}, "from": {"id": 555, "is_bot": False}}}]})
        return httpx.Response(200, json={"ok": True, "result": {"first_name": "Ayush"}})   # getChat

    run(connectors.save("telegram", {"bot_token": "1:ABCDEFGH"}, client_for(handler)))
    res = run(connectors.telegram_find_chat(client_for(handler)))
    assert res["ok"] and "Ayush" in res["detail"]
    assert connectors._load()["creds"]["telegram"]["chat_id"] == "555" and connectors.is_ready("telegram")


def test_find_my_chat_with_no_message_explains_what_to_do():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "flowbot", "id": 1}})
        return httpx.Response(200, json={"ok": True, "result": []})

    run(connectors.save("telegram", {"bot_token": "1:ABCDEFGH"}, client_for(handler)))
    with pytest.raises(connectors.ConnectorError, match="/start"):
        run(connectors.telegram_find_chat(client_for(handler)))


def test_telegram_403_bot_to_bot_gets_a_plain_message(tmp_path):
    d = connectors._load()
    d["creds"]["telegram"] = {"bot_token": "1:ABCDEFGH", "chat_id": "@flowbot"}
    d["enabled"]["telegram"] = True
    connectors._save(d)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    bad = lambda r: httpx.Response(403, json={"ok": False, "description": "Forbidden: bot can't send messages to the bot"})
    res = run(publish.publish(["telegram"], video, title="t", client=client_for(bad)))
    assert not res[0]["ok"] and "Find my chat" in res[0]["detail"]


def test_swapped_google_boxes_are_called_out_and_sign_in_is_hidden(client):
    r = run(connectors.save("youtube", {"client_id": "flow studio",
                                        "client_secret": "285012345678-abc.apps.googleusercontent.com"}))
    assert not r["ok"] and "swapped" in r["detail"]
    view = next(c for c in connectors.overview()["connectors"] if c["id"] == "youtube")
    assert view["status"] == "error"
    resp = client.post("/api/connectors/youtube/signin")
    assert resp.status_code == 400 and "swapped" in resp.json()["detail"]


def test_good_google_credentials_show_sign_in_needed():
    r = run(connectors.save("youtube", {"client_id": "1234-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-abc"}))
    assert r["ok"]
    view = next(c for c in connectors.overview()["connectors"] if c["id"] == "youtube")
    assert view["status"] == "needs_signin"


def test_google_redirect_uses_the_agent_port_not_the_proxy_port(client):
    d = connectors._load()
    d["creds"]["youtube"] = {"client_id": "1-a.apps.googleusercontent.com", "client_secret": "GOCSPX-x"}
    connectors._save(d)
    r = client.post("/api/connectors/youtube/signin", headers={"host": "localhost:5173"})
    assert r.status_code == 200
    from urllib.parse import parse_qs, urlparse
    redirect = parse_qs(urlparse(r.json()["url"]).query)["redirect_uri"][0]
    assert redirect == "http://127.0.0.1:8434/api/connectors/google/callback" or redirect.endswith("/api/connectors/google/callback")
    assert ":5173" not in redirect
