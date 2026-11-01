"""The canvas chat, used the way a person uses it.

Each test is a mistake a real run made before: guessing asset ids, repeating a step
many times (each repeat can spend credits), inventing work for a plain "hi", and not
recognising the nodes the user already had.

The AI is scripted (no network) and picture/video generation is faked.
"""
import json
from unittest.mock import patch

import httpx
import pytest
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Asset, Board, ChatMessage, ChatRun, Edge, Node
from flowboard.services import chat_agent, engines

LAKE = "11111111-1111-4111-8111-111111111111"
CLIP = "33333333-3333-4333-8333-333333333333"


def tool(name, **args):
    return json.dumps({"tool": name, "args": args})


def done(text="ok"):
    return json.dumps({"done": True, "reply": text})


def make_board():
    with get_session() as s:
        b = Board(name="t")
        s.add(b)
        s.commit()
        s.refresh(b)
        s.add(Asset(kind="image", uuid_media_id=LAKE))
        s.add(Asset(kind="video", uuid_media_id=CLIP))
        s.add(Node(board_id=b.id, short_id="lake", type="image", status="done",
                   data={"title": "Lake", "prompt": "a calm lake", "mediaId": LAKE, "mediaIds": [LAKE]}))
        s.add(Node(board_id=b.id, short_id="face", type="character", status="done",
                   data={"title": "Man", "aiBrief": "young man in a suit", "mediaId": "ffffffff-0000-4000-8000-000000000000"}))
        s.add(Node(board_id=b.id, short_id="clip", type="video", status="done",
                   data={"title": "Clip", "mediaId": CLIP, "mediaIds": [CLIP]}))
        s.add(Node(board_id=b.id, short_id="blnk", type="image", status="idle", data={}))
        s.commit()
        return b.id


async def run(board_id, message, replies, mentions=()):
    """Run the agent with scripted AI replies. Returns (status, reply text, tool log)."""
    log, it = [], iter(replies)
    counter = {"n": 0}

    async def fake_llm(prompt, attachments, turn):
        try:
            return next(it)
        except StopIteration:
            return done("(script ended)")

    async def fake_media(kind, prompt, *, reference_media_ids=None, source_media_ids=None):
        counter["n"] += 1
        mid = f"aaaaaaa{counter['n']}-0000-4000-8000-000000000000"
        with get_session() as s:
            s.add(Asset(kind=kind, uuid_media_id=mid))
            s.commit()
        log.append((kind, reference_media_ids, source_media_ids))
        return {"media_ids": [mid]}, None

    with get_session() as s:
        m = ChatMessage(board_id=board_id, role="user", content=message, mentions=list(mentions))
        s.add(m)
        s.commit()
        s.refresh(m)
        r = ChatRun(board_id=board_id, user_message_id=m.id, status="running")
        s.add(r)
        s.commit()
        s.refresh(r)
        rid = r.id
    with patch.object(chat_agent, "_chat_llm", fake_llm), patch.object(chat_agent, "_generate_media", fake_media):
        await chat_agent.run_chat_agent(board_id, rid, message, [], list(mentions))
    with get_session() as s:
        status = s.get(ChatRun, rid).status
        reply = s.exec(select(ChatMessage).where(ChatMessage.board_id == board_id, ChatMessage.role == "assistant")
                       .order_by(ChatMessage.id.desc())).first()
    return status, (reply.content if reply else ""), log


def nodes(board_id):
    with get_session() as s:
        return {n.short_id: n for n in s.exec(select(Node).where(Node.board_id == board_id)).all()}


# ── recognising the user's own nodes ─────────────────────────────────────────

def test_context_says_what_each_node_holds_and_its_asset_id():
    bid = make_board()
    ctx = chat_agent.build_board_context(bid, "make a video from #lake", [], ["lake"])
    with get_session() as s:
        lake_asset = s.exec(select(Asset).where(Asset.uuid_media_id == LAKE)).first().id
    assert f"#lake [image] status=done @(0,0) title='Lake' prompt='a calm lake' HOLDS_PICTURE(asset={lake_asset})" in ctx
    assert "looks_like='young man in a suit'" in ctx            # the description of an uploaded character
    assert "#clip [video]" in ctx and "HOLDS_VIDEO" in ctx
    assert "#blnk [image] status=idle" in ctx and "EMPTY" in ctx


def test_a_mention_that_is_not_on_the_board_is_flagged_even_if_the_ui_missed_it():
    bid = make_board()
    ctx = chat_agent.build_board_context(bid, "use #zzzz please", [], [])        # the UI passed no mentions
    assert "#zzzz → UNKNOWN" in ctx and "Do not guess" in ctx


@pytest.mark.asyncio
async def test_video_from_an_existing_picture_uses_its_node_not_a_guessed_asset_id():
    bid = make_board()
    status, reply, log = await run(bid, "turn #lake into a video", [
        tool("gen_video", prompt="slow zoom", source_node_short_id="lake"),
        done("Made a video from your lake picture."),
    ], ["lake"])
    assert status == "done" and log == [("video", None, [LAKE])]          # the lake's own picture was the start frame
    assert len(nodes(bid)) == 5                                           # a NEW video node, the old clip is untouched
    assert nodes(bid)["clip"].data["mediaIds"] == [CLIP]


@pytest.mark.asyncio
async def test_a_reference_node_guides_a_new_picture():
    bid = make_board()
    status, _reply, log = await run(bid, "robot chef like my character #face", [
        tool("gen_image", prompt="robot chef", reference_node_short_ids=["face"]),
        done("Drew it."),
    ], ["face"])
    assert status == "done"
    assert log == [("image", ["ffffffff-0000-4000-8000-000000000000"], None)]   # the character's own picture guided it
    assert len(nodes(bid)) == 5                                                  # drawn into a NEW node


@pytest.mark.asyncio
async def test_connecting_two_nodes_works_with_ids_from_the_board():
    bid = make_board()
    status, _r, _l = await run(bid, "connect #face to #lake", [tool("add_edge", from_short_id="face", to_short_id="lake", kind="ref"), done("Connected.")])
    assert status == "done"
    with get_session() as s:
        assert len(s.exec(select(Edge).where(Edge.board_id == bid)).all()) == 1


@pytest.mark.asyncio
async def test_nodes_that_hold_nothing_or_the_wrong_kind_give_a_clear_correction():
    bid = make_board()
    status, reply, log = await run(bid, "video from the blank node", [
        tool("gen_video", prompt="x", source_node_short_id="blnk"),     # holds nothing
        tool("gen_video", prompt="x", source_node_short_id="clip"),     # holds a video, a picture is needed
        tool("gen_video", prompt="x", source_node_short_id="nope"),     # not on the board
    ])
    assert log == [] and status == "failed"                             # nothing was generated, nothing spent
    assert "I could not finish" in reply


# ── protecting the user's work and credits ───────────────────────────────────

@pytest.mark.asyncio
async def test_an_existing_result_is_not_overwritten_unless_the_user_asked_to_redo_it():
    bid = make_board()
    status, _reply, log = await run(bid, "redo the lake picture", [
        tool("gen_image", prompt="new look", node_short_id="lake"),                  # refused: it holds the user's picture
        tool("gen_image", prompt="new look", node_short_id="lake", replace=True),   # allowed: an explicit redo
        done("Redone."),
    ])
    assert len(log) == 1
    media = nodes(bid)["lake"].data["mediaIds"]
    assert len(media) == 2 and media[0] != LAKE and media[1] == LAKE         # new picture first, the original kept
    assert nodes(bid)["lake"].data["mediaId"] == media[0]                    # and the new one is what is shown


@pytest.mark.asyncio
async def test_redrawing_into_one_node_with_new_prompts_stops_after_one_result():
    """The real failure: five different prompts into the same node, five paid generations."""
    bid = make_board()
    redraws = [tool("gen_image", prompt=f"stormy lake {i}", node_short_id="lake", replace=True) for i in range(8)]
    status, reply, log = await run(bid, "regenerate #lake darker", redraws, ["lake"])
    assert len(log) == 1                                                    # one generation, not eight
    assert status == "done" and reply.startswith("Done — I drew a picture in #lake")


@pytest.mark.asyncio
async def test_the_same_call_repeated_is_not_run_twice():
    bid = make_board()
    same = tool("gen_image", prompt="a cat")
    _s, reply, log = await run(bid, "a cat", [same, same, same, same])
    assert len(log) == 1 and "drew a picture" in reply


@pytest.mark.asyncio
async def test_a_plain_hello_creates_nothing_even_if_the_model_goes_overboard():
    bid = make_board()
    before = len(nodes(bid))
    spam = [tool("create_node", type="note", x=i * 100, y=0, data={"text": "hello"}) for i in range(20)]
    status, reply, _log = await run(bid, "hi", spam)
    assert len(nodes(bid)) - before == chat_agent.MAX_NEW_NODES          # capped, not twenty
    assert "stopped after" in reply


@pytest.mark.asyncio
async def test_generation_count_is_capped_per_request():
    bid = make_board()
    many = [tool("gen_image", prompt=f"picture {i}") for i in range(20)]
    _s, reply, log = await run(bid, "make pictures", many)
    assert len(log) == chat_agent.MAX_GENERATIONS and "protect your credits" in reply


@pytest.mark.asyncio
async def test_three_failures_in_a_row_stop_with_a_reason_instead_of_twelve_turns():
    bid = make_board()
    status, reply, log = await run(bid, "x", [tool("gen_video", prompt="x")] * 12)   # missing the start picture every time
    assert status == "failed" and "source_node_short_id" in reply and log == []


# ── understanding sloppy AI replies ──────────────────────────────────────────

@pytest.mark.parametrize("raw,expected_tool", [
    ('Sure! {"tool": "create_node", "args": {"type": "note"}} hope that helps', "create_node"),
    ('```json\n{"tool": "refresh", "args": {}}\n```', "refresh"),
    ('{"tool": "gen_image", "prompt": "a cat"}', "gen_image"),                       # arguments next to "tool"
    ('{"name": "refresh", "arguments": {}}', "refresh"),                              # another naming habit
    ('{"tool": "refresh", "args": {}} {"tool": "create_node", "args": {}}', "refresh"),   # two objects: take the first
])
def test_sloppy_but_understandable_replies_are_parsed(raw, expected_tool):
    assert chat_agent._parse_tool_call(raw).tool == expected_tool


def test_replies_with_no_usable_object_are_rejected_and_done_is_recognised():
    assert chat_agent._parse_tool_call("I think we should do something") is None
    assert chat_agent._parse_tool_call('{"tool": ""}') is None
    d = chat_agent._parse_tool_call('Done! {"done": true, "reply": "All set"}')
    assert d.done and d.reply == "All set"


# ── AI engine trouble ────────────────────────────────────────────────────────

@pytest.fixture
def one_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "STORE_PATH", tmp_path / "e.json")
    monkeypatch.setattr(engines, "_RETRY_WAIT", 0)
    models = engines.analyse_models("groq", ["llama-3.3-70b-versatile"])
    engines._save({"keys": {"groq": "gsk_a"}, "state": {"groq": {"status": "ok", "models": models}}, "assignments": {}})


@pytest.mark.asyncio
async def test_a_rate_limit_is_retried_once_before_giving_up(one_engine):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await engines.chat("chat", "hi", client=c) == "hello" and len(calls) == 2


@pytest.mark.asyncio
async def test_a_connected_engine_that_keeps_failing_is_reported_honestly_not_as_missing(one_engine):
    from flowboard.services.llm import registry
    from flowboard.services.llm.base import LLMError
    c = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(429)))
    real_chat = engines.chat

    async def chat_with_client(job, user, **kw):
        return await real_chat(job, user, client=c, **{k: v for k, v in kw.items() if k != "client"})
    with patch.object(registry.engines, "chat", chat_with_client):
        with pytest.raises(LLMError) as exc:
            await registry.run_llm("chat_agent", "hello")
    assert "did not answer" in str(exc.value) and "rate limit" in str(exc.value)
    assert "No AI provider configured" not in str(exc.value)


@pytest.mark.asyncio
async def test_replace_is_ignored_unless_the_user_asked_to_redo_something():
    """The AI asked to overwrite the user's existing clip although they only said 'make a video'."""
    bid = make_board()
    status, _reply, log = await run(bid, "turn #lake into a short video", [
        tool("gen_video", prompt="zoom", source_node_short_id="lake", node_short_id="clip", replace=True),   # refused
        tool("gen_video", prompt="zoom", source_node_short_id="lake"),                                      # a new node instead
        done("Made a new video."),
    ], ["lake"])
    assert status == "done" and len(log) == 1
    assert nodes(bid)["clip"].data["mediaIds"] == [CLIP]       # the user's clip is untouched
    assert len(nodes(bid)) == 5                                # a new video node was added


@pytest.mark.asyncio
async def test_replace_is_honoured_when_the_user_says_regenerate():
    bid = make_board()
    status, _r, log = await run(bid, "regenerate #lake darker", [
        tool("gen_image", prompt="darker lake", node_short_id="lake", replace=True), done("Redone.")], ["lake"])
    assert status == "done" and len(log) == 1 and len(nodes(bid)["lake"].data["mediaIds"]) == 2
