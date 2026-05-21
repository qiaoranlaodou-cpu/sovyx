"""End-to-end regression for Mission B B-P0-1 — frontend ack URL parity.

Mission anchor: ``docs-internal/MISSION-B-FINDINGS-REGISTER-2026-05-21.md`` §1
(B-P0-1) + ``docs-internal/MISSION-B-REMEDIATION-PLAN-2026-05-21.md`` §5
(B.1.P1).

Forensic context. Between v0.46.4 (commit ``035bc600``, 2026-05-17) and
v0.49.36 (commit ``2985245a``, 2026-05-21):

* The C4 mission spec §T3.3 wrote the path in PROSE as
  ``/api/voice/degraded/ack`` and in its CODE BLOCK as
  ``@router.post("/degraded/ack")`` inside a router whose prefix is
  ``/api/engine``. Effective route: ``/api/engine/degraded/ack``.
* The frontend hook (``use-engine-degraded-poller.ts:60``) POSTed to the
  PROSE path. FastAPI returned 404. ``DegradedBannerGlobalMount.tsx:27``
  ``.catch(() => {})`` swallowed the error silently.
* 16 sibling sites (CHANGELOG, server docstrings, vitest mock assertions,
  test docstrings, mission-spec, operator-validation backlog curl) all
  agreed on the prose path. Each side's own unit test passed because each
  side tested its own literal.

Mission B classified this as a **spec-transcription propagation failure
class**, NOT a typo. The gap that allowed it: producer↔consumer URL
parity is not gated. Anti-pattern #53 (proposed) codifies the rule;
Quality Gate 16 (proposed) STRICT-enforces.

This regression test pins the exact frontend literal against the
TestClient-resolved server route. Pre-B.1.P1 fix this test would have
returned 404; post-fix it returns 200 or 503 (no axes registered in the
minimal test app — both are valid responses meaning the route resolved).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from sovyx.dashboard.server import create_app

_TOKEN = "test-token-b-p0-1-regression"

# Frontend literal — read this constant whenever auditing parity.
# Updated by Mission B.1.P1 (2026-05-21) to match the runtime-registered
# route. The frontend `ackComposite` helper at
# `dashboard/src/hooks/use-engine-degraded-poller.ts:60` MUST stay in
# sync with this constant. Anti-pattern #53.
_FRONTEND_ACK_PATH = "/api/engine/degraded/ack"


def test_frontend_ack_endpoint_is_registered() -> None:
    """The path the frontend POSTs to MUST be a real route.

    Mission B B-P0-1 falsifiability. Pre-fix the frontend literal at
    ``use-engine-degraded-poller.ts:60`` was ``"/api/voice/degraded/ack"``
    which FastAPI did not register; this test would have returned 404.

    Post-fix the frontend literal is ``"/api/engine/degraded/ack"``;
    response should be 200 (store accepted ack) or 503 (no axes
    registered in this minimal test app). Anything else — especially
    404 — means the parity contract drifted again.
    """
    app = create_app(token=_TOKEN)
    client = TestClient(
        app,
        headers={"Authorization": f"Bearer {_TOKEN}"},
    )
    response = client.post(
        _FRONTEND_ACK_PATH,
        json={"reason": "composite", "ttl_sec": 3600},
    )
    assert response.status_code != 404, (
        f"Frontend POST to {_FRONTEND_ACK_PATH} returned 404 — the "
        f"frontend literal at use-engine-degraded-poller.ts:60 has "
        f"drifted from the server registration in engine_degraded.py "
        f"(anti-pattern #53). Audit the 16-site drift inventory at "
        f"MISSION-B-FINDINGS-REGISTER-2026-05-21.md §1 B-P0-1."
    )
    assert response.status_code in (200, 503), (
        f"Frontend POST to {_FRONTEND_ACK_PATH} returned unexpected "
        f"status {response.status_code} (body: {response.text!r}). "
        f"Expected 200 (ack recorded) or 503 (no axes / store "
        f"unavailable in minimal test app)."
    )


def test_frontend_literal_value_is_under_api_engine_prefix() -> None:
    """Lightweight literal-shape guard for anti-pattern #53.

    Mission B B-P0-1 root cause was the C4 spec writing the path under
    `/api/voice/...` while the decorator lived under `/api/engine/...`.
    Until the proposed Quality Gate 16 ships (Mission B.5.P1 — frontend
    POST/PUT/PATCH/DELETE path parity scanner), this micro-guard catches
    the most likely re-drift direction: a future contributor moving the
    constant back to the `/api/voice/...` prose family.
    """
    assert _FRONTEND_ACK_PATH.startswith("/api/engine/"), (
        f"_FRONTEND_ACK_PATH={_FRONTEND_ACK_PATH!r} is not under the "
        f"/api/engine prefix. The C4 composite-ack store is cross-axis "
        f"(llm/stt/voice/dashboard/engine_resources/...) — placing the "
        f"endpoint under /api/voice/ would semantically lie about its "
        f"scope and recreate the B-P0-1 drift class."
    )
