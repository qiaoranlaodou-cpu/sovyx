"""Composite engine-degraded surface across voice + LLM + STT axes.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.6.

Replaces N independent log-grep workflows with one actionable payload.
Operators no longer need to correlate `bootstrap.py:735`
``no_llm_provider_detected`` + `voice/factory/_validate.py:542`
``voice.factory.stt_language_unsupported`` + `voice/health/_runtime_failover.py`
``voice.failover.ladder_complete{verdict=exhausted}`` by hand — the
composite endpoint surfaces all three (and any future degraded axis)
in one payload + drives the global dashboard banner.

Severity escalation per ADR-D6:

* 0 axes → ``composite_severity = None`` (banner hidden).
* 1 axis  → ``"warn"``.
* 2 axes → ``"error"``.
* 3+ axes OR auto-restart governor exhausted (Phase 2 §T2.2) →
  ``"critical"``.

Anti-pattern compliance:

* #18 — exposed through ``api.*`` JSON helper on the frontend
  (no raw ``fetch()`` consumers).
* #40 — paired Quality Gate 8 round-trip test at
  ``tests/dashboard/test_engine_degraded_boundary.py``.
* #42 — single composite surface; producers MUST consult
  :mod:`sovyx.engine._degraded_store` rather than emit independent
  log lines the operator must correlate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from sovyx.dashboard.routes._deps import verify_token
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
)
from sovyx.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/engine", dependencies=[Depends(verify_token)])


class ActionChipModel(BaseModel):
    """Operator-actionable button chip rendered inside the banner.

    Mirror of :class:`sovyx.engine._degraded_store.ActionChip` — the
    pydantic-side declaration keeps the OpenAPI schema explicit + the
    frontend zod twin (``ActionChipSchema`` in
    ``dashboard/src/types/schemas.ts``) gets a clean type to lock onto.
    """

    model_config = {"extra": "allow"}

    label_token: str
    action: str
    target: str
    style: str = "default"


class DegradedAxisModel(BaseModel):
    """One axis entry in the composite payload.

    Forward-additive: future axes (brain.embedding_model_unavailable,
    bridges.channel_failed, plugin.sandbox_quota_hit) extend this
    schema without a route migration thanks to
    ``model_config = {"extra": "allow"}``.
    """

    model_config = {"extra": "allow"}

    axis: str
    reason: str
    severity: str
    title_token: str
    body_token: str
    action_chips: list[ActionChipModel] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    first_observed_monotonic: float
    last_observed_monotonic: float
    occurrence_count: int


class AckStateModel(BaseModel):
    """Operator-acknowledgement state for the composite banner.

    Phase 1 ships the schema with default-empty values; Phase 3
    (``operator_acks`` SQLite table + ``POST /api/voice/degraded/ack``)
    populates the fields from
    :mod:`sovyx.engine._operator_acks_store`.

    Forward-additive — future fields (``ack_reason``, ``last_resurfaced_at``,
    ``operator_token_hash``) extend without a schema migration.
    """

    model_config = {"extra": "allow"}

    acked: bool = False
    acked_at_ts: int | None = None
    ttl_sec: int | None = None
    ttl_remaining_sec: int | None = None
    operator_id: str | None = None


class EngineDegradedResponse(BaseModel):
    """Top-level composite payload for ``GET /api/engine/degraded``.

    Consumed by the global ``<DegradedBannerGlobalMount>`` + per-page
    ``<DegradedBannerPerPageMount>`` components (Mission C4 §T1.10 /
    §T1.11) via the ``useEngineDegradedPoller`` hook.
    """

    model_config = {"extra": "allow"}

    axes: list[DegradedAxisModel] = Field(default_factory=list)
    composite_severity: str | None = None
    composite_axis_count: int = 0
    ack: AckStateModel = Field(default_factory=AckStateModel)


def _compute_composite_severity(distinct_axis_count: int) -> str | None:
    """Severity escalation per ADR-D6.

    Kept as a free function (not an enum) so the producer-side
    consumer at ``voice_status.get_voice_status`` can call it directly
    without a circular import on the response model.
    """
    if distinct_axis_count <= 0:
        return None
    if distinct_axis_count == 1:
        return "warn"
    if distinct_axis_count == 2:
        return "error"
    return "critical"


def _entry_to_axis_model(entry: DegradedEntry) -> DegradedAxisModel:
    return DegradedAxisModel(
        axis=entry.axis,
        reason=entry.reason,
        severity=entry.severity,
        title_token=entry.title_token,
        body_token=entry.body_token,
        action_chips=[
            ActionChipModel(
                label_token=c.label_token,
                action=c.action,
                target=c.target,
                style=c.style,
            )
            for c in entry.action_chips
        ],
        metadata=entry.metadata,
        first_observed_monotonic=entry.first_observed_monotonic,
        last_observed_monotonic=entry.last_observed_monotonic,
        occurrence_count=entry.occurrence_count,
    )


@router.get("/degraded", response_model=EngineDegradedResponse)
async def get_engine_degraded() -> EngineDegradedResponse:
    """Composite degraded-state snapshot across all engine axes.

    Mission C4 §T1.6 — the single source-of-truth for the dashboard
    banner mount. Replaces N independent log-grep workflows with one
    actionable payload.

    Auth via the shared ``verify_token`` dependency on the router.
    Idempotent + cheap (in-memory snapshot of ``EngineDegradedStore``);
    safe to poll at 5 s cadence under the
    :func:`useEngineDegradedPoller` hook.
    """
    store = get_default_degraded_store()
    entries = store.snapshot()
    distinct_axes = sorted({e.axis for e in entries})

    return EngineDegradedResponse(
        axes=[_entry_to_axis_model(e) for e in entries],
        composite_severity=_compute_composite_severity(len(distinct_axes)),
        composite_axis_count=len(distinct_axes),
        ack=AckStateModel(),
    )


__all__ = [
    "ActionChipModel",
    "AckStateModel",
    "DegradedAxisModel",
    "EngineDegradedResponse",
    "_compute_composite_severity",
    "router",
]
