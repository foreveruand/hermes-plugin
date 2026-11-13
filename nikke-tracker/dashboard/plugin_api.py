#!/usr/bin/env python3
"""FastAPI routes for the NIKKE dashboard plugin.

Mounted at /api/plugins/nikke-tracker/.  The real work lives in nikke_core.py;
these handlers only translate exceptions to HTTP status codes.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse

_SPEC = importlib.util.spec_from_file_location(
    "nikke_tracker_core", Path(__file__).with_name("nikke_core.py")
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - packaging error
    raise RuntimeError("无法加载 nikke_core.py")
core = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(core)

router = APIRouter()
log = logging.getLogger("hermes.plugin.nikke_tracker")


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except core.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except core.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except core.ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except core.PluginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the browser
        log.exception("NIKKE plugin request failed")
        raise HTTPException(status_code=500, detail=f"插件内部错误: {exc}") from exc


@router.get("/health")
def health():
    return core.health()


@router.get("/bootstrap")
def bootstrap():
    return _call(core.bootstrap)


@router.get("/detail")
def detail(name: str = Query(..., min_length=1)):
    return _call(core.detail, name)


@router.put("/gear")
def save_gear(body: dict[str, Any] = Body(...)):
    return _call(
        core.save_gear,
        body.get("name", ""),
        body.get("slot", ""),
        body.get("level"),
        body.get("slots"),
        body.get("rev"),
    )


@router.put("/gear/batch")
def save_gear_batch(body: dict[str, Any] = Body(...)):
    return _call(
        core.save_gear_batch,
        body.get("name", ""),
        body.get("pieces"),
        body.get("rev"),
    )


@router.put("/meta")
def save_meta(body: dict[str, Any] = Body(...)):
    payload = dict(body)
    name = payload.pop("name", "")
    rev = payload.pop("rev", None)
    return _call(core.save_meta, name, payload, rev)


@router.put("/target")
def save_target(body: dict[str, Any] = Body(...)):
    payload = dict(body)
    name = payload.pop("name", "")
    targets_rev = payload.pop("targets_rev", None)
    return _call(core.save_target, name, payload, targets_rev)


@router.post("/character")
def add_character(body: dict[str, Any] = Body(...)):
    return _call(core.add_character, body.get("name", ""), body.get("rev"))


@router.delete("/character/{name}")
def remove_character(
    name: str,
    confirm: bool = Query(False),
    rev: str | None = Query(None),
):
    return _call(core.remove_character, name, confirm, rev)


@router.put("/state")
def set_state(body: dict[str, Any] = Body(...)):
    return _call(core.set_favorites, body.get("favorites"))


@router.post("/render")
def render():
    return _call(core.render)


@router.get("/teams")
def teams():
    return _call(core.teams)


@router.post("/portrait/upload")
def upload_portrait(body: dict[str, Any] = Body(...)):
    return _call(core.save_portrait, body.get("name", ""), body.get("data_url", ""))


@router.post("/portrait/fetch")
def fetch_portrait(body: dict[str, Any] = Body(...)):
    return _call(core.fetch_portrait, body.get("name", ""))


@router.get("/portrait/{key}")
def portrait(key: str):
    try:
        path, mime = core.portrait_file(key)
    except core.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=3600"},
    )


__all__ = ["router"]