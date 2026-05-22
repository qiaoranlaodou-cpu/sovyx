"""Unified activity timeline endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


class ActivityTimelineMetaModel(BaseModel):
    """Meta block on the activity timeline response (Mission C C.4)."""

    model_config = ConfigDict(extra="allow")
    hours: int
    limit: int
    total_before_limit: int = 0
    cutoff: str = ""


class ActivityTimelineResponse(BaseModel):
    """Response of `GET /api/activity/timeline` (Mission C C.4).

    Forward-additive via ``extra="allow"`` (anti-pattern #40). Entries
    typed as untyped dicts because the per-entry shape varies by
    activity source (saga / chat / brain-update); a future C-phase
    can narrow this once each source has a stable model."""

    model_config = ConfigDict(extra="allow")
    entries: list[dict[str, object]] = []
    meta: ActivityTimelineMetaModel


@router.get("/activity/timeline", response_model=ActivityTimelineResponse)
async def get_activity_timeline(
    request: Request,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=100, ge=1, le=500),
) -> JSONResponse:
    """Unified cognitive activity timeline from persistent storage."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.activity import get_activity_timeline as _get_timeline

        timeline = await _get_timeline(registry, hours=hours, limit=limit)
        return JSONResponse(timeline)
    empty_meta = {"hours": hours, "limit": limit, "total_before_limit": 0, "cutoff": ""}
    return JSONResponse({"entries": [], "meta": empty_meta})
