"""Background Flow routes — start/stop the separate Flow window and read its status."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from flowboard.services import flow_runner

router = APIRouter(prefix="/api/flow-runner", tags=["flow-runner"])


class StartBody(BaseModel):
    mode: str = "background"        # "setup" (visible, one time) or "background"


class ConfigBody(BaseModel):
    browser: Optional[str] = None
    keep_running: Optional[bool] = None
    flow_url: Optional[str] = None


class SetupBody(BaseModel):
    done: bool = True


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
def status() -> dict:
    return flow_runner.status()


@router.post("/heartbeat")
def heartbeat() -> dict:
    return flow_runner.heartbeat()


@router.post("/start")
def start(body: StartBody) -> dict:
    try:
        return flow_runner.start(body.mode)
    except flow_runner.RunnerError as exc:
        raise _bad(exc)


@router.post("/stop")
def stop() -> dict:
    return flow_runner.stop()


@router.put("/setup")
def setup(body: SetupBody) -> dict:
    return flow_runner.mark_setup_done(body.done)


@router.put("/config")
def config(body: ConfigBody) -> dict:
    try:
        return flow_runner.configure(body.browser, body.keep_running, body.flow_url)
    except flow_runner.RunnerError as exc:
        raise _bad(exc)
