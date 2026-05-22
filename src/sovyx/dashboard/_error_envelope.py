"""Shared error response envelope for dashboard routes.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.4
(``ErrorEnvelope`` SSoT recommendation; closes C-P0-4 + half of C-P0-14
OpenAPI half).

Single pydantic model used as the ``responses={NNN: {"model":
ErrorEnvelope}}`` payload across every dashboard route's 4xx/5xx
branches. Without this SSoT, generated clients see the bare FastAPI
``HTTPException`` default (``{"detail": str}``) — opaque to the
operator + indistinguishable across error classes.

The :class:`ErrorCode` enum is the stable machine-readable error
identity. Frontend ``apiFetch`` error-path consumers branch on
``err.body.error_code`` (sibling of anti-pattern #46 — SSoT enum for
operator-actionable categorical channels).

Why a separate module (not co-located with a route):

* Used across all route files; co-location would force circular
  imports or duplicate definitions.
* Single edit point for new error codes — anti-pattern #56 (proposed)
  alignment.
* Importable from CLI doctor surfaces + plugin SDKs that want to
  parse engine-side error envelopes without re-deriving the schema.

Anti-pattern compliance:

* #20 — module under ``src/sovyx/dashboard/`` (operational), not in
  ``src/sovyx/engine/``.
* #40 — boundary-test paired at
  ``tests/unit/dashboard/test_error_envelope_boundary.py``.
* #56 (proposed; closure via Phase C.8 enum SSoT).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    """Stable machine-readable error identity.

    Members are operator-actionable categories. The wire string is the
    member value (StrEnum auto-coercion); frontend ``err.body.error_code``
    branches on these literals.

    Forward-additive: new members ship in CONCERT with the producer
    edit + the zod twin update + the i18n key set (the same
    anti-pattern #46 / SSoT-rename discipline applies).
    """

    # Generic categories — every route's HTTPException defaults to one
    # of these unless the route opts into a more specific code.
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNPROCESSABLE = "unprocessable"
    TOO_MANY_REQUESTS = "too_many_requests"
    INTERNAL = "internal"
    SERVICE_UNAVAILABLE = "service_unavailable"
    GATEWAY_TIMEOUT = "gateway_timeout"

    # Engine-state categories — common dashboard-route 503 reasons.
    ENGINE_NOT_RUNNING = "engine_not_running"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"


class ErrorEnvelope(BaseModel):
    """Stable error response shape for every dashboard route's 4xx/5xx.

    Wire shape::

        {
            "error": "Human-readable summary",
            "error_code": "service_unavailable",
            "detail": "Optional longer-form context",
            "retry_after_seconds": 30
        }

    All fields are optional EXCEPT ``error`` + ``error_code``. The
    ``detail`` field carries dynamic per-incident context; the
    ``retry_after_seconds`` field is operator-actionable on retryable
    failures (matches the HTTP ``Retry-After`` header contract).

    Forward-additive (``extra="allow"``) so future axes (e.g.
    ``correlation_id``, ``operator_action_chip``) can extend without a
    breaking migration.
    """

    model_config = ConfigDict(extra="allow")

    error: str = Field(
        description=(
            "Human-readable summary. Operator-visible in the dashboard "
            "error banner; should be specific enough to drive remediation."
        ),
    )
    error_code: ErrorCode = Field(
        description=(
            "Stable machine-readable category. Frontend branches on this "
            "literal; new categories extend :class:`ErrorCode` in concert "
            "with the producer + zod twin."
        ),
    )
    detail: str | None = Field(
        default=None,
        description=(
            "Optional longer-form context (stack-trace summary, "
            "underlying-exception class, dependency-side error message). "
            "Operator-visible in expanded banner state."
        ),
    )
    retry_after_seconds: int | None = Field(
        default=None,
        ge=0,
        le=86400,
        description=(
            "Operator-actionable retry hint for transient failures. "
            "Matches the HTTP ``Retry-After`` header semantics. None when "
            "the failure is terminal or the retry interval is "
            "indeterminate."
        ),
    )


def envelope(
    error: str,
    error_code: ErrorCode,
    *,
    detail: str | None = None,
    retry_after_seconds: int | None = None,
) -> dict[str, object]:
    """Build an ErrorEnvelope-shaped dict for HTTPException.detail.

    Convenience helper so route handlers can write::

        raise HTTPException(
            status_code=503,
            detail=envelope("LLM router unavailable", ErrorCode.SERVICE_UNAVAILABLE),
        )

    The dict round-trips cleanly through ErrorEnvelope.model_validate so
    boundary tests can assert the shape end-to-end.
    """
    payload: dict[str, object] = {
        "error": error,
        "error_code": error_code.value,
    }
    if detail is not None:
        payload["detail"] = detail
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = retry_after_seconds
    return payload


__all__ = [
    "ErrorCode",
    "ErrorEnvelope",
    "envelope",
]
