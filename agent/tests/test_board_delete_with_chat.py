"""Deleting a board must work after the chat has been used on it.

A ChatRun and a ChatAttachment both point at a ChatMessage, so deleting the
messages first used to fail with a foreign-key error.
"""
from sqlmodel import select

from flowboard.db import get_session
from flowboard.db.models import Asset, ChatAttachment, ChatMessage, ChatRun


def test_delete_board_with_chat_runs_and_attachments(client):
    board = client.post("/api/boards", json={"name": "chatty"}).json()
    with get_session() as s:
        msg = ChatMessage(board_id=board["id"], role="user", content="make a video")
        asset = Asset(kind="image", local_path="x.png")
        s.add(msg)
        s.add(asset)
        s.commit()
        s.refresh(msg)
        s.refresh(asset)
        s.add(ChatRun(board_id=board["id"], user_message_id=msg.id, status="done"))
        s.add(ChatAttachment(message_id=msg.id, asset_id=asset.id))
        s.commit()

    r = client.delete(f"/api/boards/{board['id']}")
    assert r.status_code == 200 and r.json() == {"deleted": board["id"]}
    with get_session() as s:
        assert s.exec(select(ChatMessage)).all() == []
        assert s.exec(select(ChatRun)).all() == []
        assert s.exec(select(ChatAttachment)).all() == []
