"""Engines routes — bring-your-own-key AI providers.

Keys go in, are tested and stored locally; only a masked hint ever comes back.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from flowboard.services import engines

router = APIRouter(prefix="/api/engines", tags=["engines"])


class KeyBody(BaseModel):
    key: str = Field(min_length=8, max_length=400)


class DetectBody(BaseModel):
    key: str = Field(min_length=1, max_length=400)


class AssignBody(BaseModel):
    engine: Optional[str] = None
    model: Optional[str] = None     # exact model for that engine; empty = recommended
    backup: Optional[str] = None    # engine to try if the first one fails


def _known(pid: str) -> None:
    if pid not in engines.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown engine")


@router.get("")
def overview() -> dict:
    return engines.overview()


@router.post("/detect")
def detect(body: DetectBody) -> dict:
    pid = engines.detect_provider(body.key)
    return {"engine": pid, "label": engines.PROVIDERS[pid]["label"] if pid else None}


@router.put("/{pid}/key")
async def set_key(pid: str, body: KeyBody) -> dict:
    _known(pid)
    result = await engines.save_key(pid, body.key)
    return {"result": {k: v for k, v in result.items() if k != "models"}, "overview": engines.overview()}


@router.post("/{pid}/test")
async def test(pid: str) -> dict:
    _known(pid)
    try:
        result = await engines.retest(pid)
    except engines.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"result": {k: v for k, v in result.items() if k != "models"}, "overview": engines.overview()}


@router.delete("/{pid}/key")
def delete_key(pid: str) -> dict:
    _known(pid)
    engines.remove_key(pid)
    return engines.overview()


@router.put("/assign/{job}")
def assign(job: str, body: AssignBody) -> dict:
    try:
        engines.set_assignment(job, body.engine, body.model, body.backup)
    except engines.EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return engines.overview()
