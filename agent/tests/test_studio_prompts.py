"""Studio pictures must fill the whole frame.

An earlier prompt asked the model to "leave the top 28 percent empty", and it drew that as a blank
box on every picture. Calm space at the top is now only requested per panel, by the script.
"""
from flowboard.routes import studio


def test_styles_ask_for_full_bleed_art_and_no_empty_band(client):
    body = client.get("/api/studio/styles").json()
    suffix = body["suffix"]
    assert "Full-bleed" in suffix and "no empty rectangles" in suffix and "no white or blank bands" in suffix
    assert "28 percent" not in suffix and "Leave the top" not in suffix        # the old, harmful instruction
    assert "NO text" in suffix and "NO speech bubbles" in suffix              # bubbles are drawn by Studio
    # No style preset may nudge the model toward a bordered "panel".
    assert all("panel" not in v.lower() for v in body["styles"].values())
    assert set(body["styles"]) == {"comic", "stickman", "anime", "watercolor"}


def test_headroom_is_an_opt_in_part_of_the_artwork_not_a_box(client):
    h = client.get("/api/studio/styles").json()["headroom_suffix"]
    assert "upper third" in h and "not a box" in h and "part of the same artwork" in h


def test_the_script_prompt_tells_the_ai_when_to_leave_space():
    sys = studio._SCRIPT_SYSTEM
    assert '"space": "none"|"top"' in sys
    assert "almost every panel" in sys and "ONLY when" in sys
    assert "Never describe borders" in sys


def test_the_offline_script_defaults_every_panel_to_full_frame(client, monkeypatch):
    from flowboard.services.llm.base import LLMError

    async def no_ai(job, user, system, timeout):
        raise LLMError("no AI available in this test")     # never reach a real engine or CLI from a test
    monkeypatch.setattr(studio, "_llm", no_ai)
    r = client.post("/api/studio/script", json={"idea": "two robots race a snail", "panels": 4}).json()
    assert r["source"] == "template" and len(r["panels"]) == 4
    assert all(p["space"] == "none" for p in r["panels"])


def test_flow_link_in_the_idea_pins_the_project_and_is_removed(client, monkeypatch):
    from flowboard.routes import studio

    async def fake(*a, **k):
        return '{"title": "T", "characters": "c", "panels": [{"scene": "s", "caption": "", "bubbles": [], "sfx": ""}]}'

    monkeypatch.setattr(studio, "_llm", fake)
    pid = "cf2ec93b-14a5-4fac-bfe8-91a20d579790"
    r = client.post("/api/studio/script", json={"idea": f"https://flow.google.com/project/{pid} zombie eclipse comic", "panels": 3}).json()
    assert r["pinned_project"] == pid and r["idea"] == "zombie eclipse comic"
    assert client.get("/api/flow/projects/pinned").json()["flow_project_id"] == pid


def test_link_only_idea_pins_but_asks_for_a_story(client):
    pid = "36c458aa-d5b3-4cb6-9cbb-3b34fb68c46c"
    r = client.post("/api/studio/script", json={"idea": f"https://flow.google.com/project/{pid}", "panels": 3})
    assert r.status_code == 400 and "story" in r.json()["detail"]
    assert client.get("/api/flow/projects/pinned").json()["flow_project_id"] == pid
