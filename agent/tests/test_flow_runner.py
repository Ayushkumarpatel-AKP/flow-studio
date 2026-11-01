"""Background Flow runner: setup gate, status states, watchdog limits (no real browser is started)."""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from flowboard.services import flow_runner


class FakeProc:
    pid = 4242

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(flow_runner, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(flow_runner, "_proc", None)
    monkeypatch.setattr(flow_runner, "_want_running", False)
    monkeypatch.setattr(flow_runner, "_suppressed", False)
    monkeypatch.setattr(flow_runner, "_lease_until", 0.0)
    monkeypatch.setattr(flow_runner, "find_browsers", lambda: {"chrome": "C:/fake/chrome.exe"})
    monkeypatch.setattr(flow_runner.flow_client, "_ws", None, raising=False)
    monkeypatch.setattr(flow_runner, "_pid_alive", lambda pid: False)
    launched = []

    def fake_popen(args, **kw):
        launched.append(args)
        return FakeProc()

    monkeypatch.setattr(flow_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(flow_runner, "_kill_tree", lambda pid: launched.append(("kill", pid)))
    return launched


def test_needs_setup_before_background(client):
    st = client.get("/api/flow-runner").json()
    assert st["state"] == "needs_setup" and st["browsers"] == ["chrome"]
    r = client.post("/api/flow-runner/start", json={"mode": "background"})
    assert r.status_code == 400 and "setup" in r.json()["detail"].lower()


def test_setup_window_is_visible_and_background_is_off_screen(_isolate):
    flow_runner.start("setup")
    assert not any("--window-position" in a for a in _isolate[0])
    assert any(a.startswith("--user-data-dir=") for a in _isolate[0])
    flow_runner.stop()
    flow_runner.mark_setup_done()
    flow_runner.start("background")
    args = _isolate[-1] if isinstance(_isolate[-1], list) else _isolate[-2]
    assert "--window-position=-2400,-2400" in args and "--disable-renderer-backgrounding" in args
    assert args[-1] == "https://flow.google.com/"


def test_status_states(monkeypatch):
    flow_runner.mark_setup_done()
    assert flow_runner.status()["state"] == "stopped"
    monkeypatch.setattr(flow_runner, "is_running", lambda: True)
    assert flow_runner.status()["state"] == "waiting"
    monkeypatch.setattr(flow_runner.flow_client.__class__, "connected", property(lambda self: True))
    assert flow_runner.status()["state"] == "working"
    monkeypatch.setattr(flow_runner, "is_running", lambda: False)
    assert flow_runner.status()["state"] == "other_browser"


def test_cannot_start_twice(monkeypatch):
    flow_runner.mark_setup_done()
    flow_runner.start("background")
    monkeypatch.setattr(flow_runner, "is_running", lambda: True)
    with pytest.raises(flow_runner.RunnerError, match="already"):
        flow_runner.start("background")


def test_config_validates_browser_and_flow_address(client):
    assert client.put("/api/flow-runner/config", json={"browser": "firefox"}).status_code == 400
    assert client.put("/api/flow-runner/config", json={"flow_url": "https://evil.example/"}).status_code == 400
    r = client.put("/api/flow-runner/config", json={"browser": "chrome", "keep_running": True,
                                                    "flow_url": "https://flow.google.com/project/abc"})
    assert r.status_code == 200 and r.json()["keep_running"] is True


def _run_watchdog(monkeypatch, loops=40):
    real_sleep = asyncio.sleep

    async def fast(_):
        await real_sleep(0)

    monkeypatch.setattr(flow_runner.asyncio, "sleep", fast)

    async def go():
        task = asyncio.create_task(flow_runner._watch())
        for _ in range(loops):
            await real_sleep(0)
        task.cancel()

    asyncio.run(go())


def test_closing_the_window_never_reopens_it(monkeypatch, _isolate):
    flow_runner.mark_setup_done()
    flow_runner.start("background")
    launches = len([a for a in _isolate if isinstance(a, list)])
    monkeypatch.setattr(flow_runner, "is_running", lambda: False)        # the user closed it
    _run_watchdog(monkeypatch)
    assert len([a for a in _isolate if isinstance(a, list)]) == launches   # not reopened
    assert flow_runner._want_running is False and flow_runner._suppressed is True


def test_window_is_closed_when_studio_goes_away(monkeypatch, _isolate):
    flow_runner.mark_setup_done()
    flow_runner.start("background")
    monkeypatch.setattr(flow_runner, "is_running", lambda: True)
    monkeypatch.setattr(flow_runner, "_current_pid", lambda: 4242)
    flow_runner._lease_until = 0          # no heartbeat for a long time and no job
    _run_watchdog(monkeypatch)
    assert ("kill", 4242) in _isolate and flow_runner._want_running is False


def test_window_stays_while_studio_is_open(monkeypatch, _isolate):
    flow_runner.mark_setup_done()
    flow_runner.start("background")
    monkeypatch.setattr(flow_runner, "is_running", lambda: True)
    flow_runner.touch()
    _run_watchdog(monkeypatch)
    assert not any(isinstance(a, tuple) for a in _isolate) and flow_runner._want_running is True


def test_heartbeat_opens_only_on_a_fresh_visit_and_when_enabled(monkeypatch, _isolate):
    flow_runner.mark_setup_done()
    flow_runner._lease_until = 0
    flow_runner.heartbeat()                                            # setting is off: nothing opens
    assert not any(isinstance(a, list) for a in _isolate)
    flow_runner.configure(keep_running=True)
    flow_runner._lease_until = 0
    flow_runner.heartbeat()                                            # fresh visit: opens
    assert len([a for a in _isolate if isinstance(a, list)]) == 1
    monkeypatch.setattr(flow_runner, "is_running", lambda: False)
    flow_runner.heartbeat()                                            # same visit: does not reopen
    assert len([a for a in _isolate if isinstance(a, list)]) == 1


def test_a_user_closed_window_stays_closed_until_a_new_visit(monkeypatch, _isolate):
    flow_runner.mark_setup_done()
    flow_runner.configure(keep_running=True)
    flow_runner._lease_until = 0
    flow_runner.heartbeat()
    monkeypatch.setattr(flow_runner, "is_running", lambda: False)
    _run_watchdog(monkeypatch)                                         # user closed it
    flow_runner.heartbeat()                                            # Studio still open: stays closed
    assert len([a for a in _isolate if isinstance(a, list)]) == 1
    flow_runner._lease_until = 0                                       # Studio was closed, then opened again
    flow_runner.heartbeat()
    assert len([a for a in _isolate if isinstance(a, list)]) == 2


def test_starting_the_agent_never_opens_a_browser(monkeypatch, _isolate):
    flow_runner.mark_setup_done()
    flow_runner.configure(keep_running=True)

    async def go():
        flow_runner.start_watchdog()
        flow_runner.stop_watchdog()

    asyncio.run(go())
    assert not any(isinstance(a, list) for a in _isolate)
