"""L3 regression — ``startup.network`` carries zero dashboard bind fields.

FORENSIC §L3 — operator's 2026-05-14 v0.43.1 session line 35 emitted
``startup.network ... network.dashboard_host=None network.dashboard_port=None``
inside the ``startup`` saga (``kind=diagnosis``, L13). The audit
classified this as a cosmetic temporal-binding issue ("snapshot
captured before dashboard task wired its bind address"). The root
cause is deeper: ``_emit_network`` read ``config.dashboard.host`` /
``config.dashboard.port`` but ``EngineConfig`` has no top-level
``dashboard`` attribute — the API bind lives at
``config.api.host`` / ``config.api.port`` (`engine/config.py:3256`),
and a separate ``DashboardTuningConfig`` lives at
``config.tuning.dashboard`` (`engine/config.py:3025`). The
``getattr(config, "dashboard", None)`` lookup therefore returned
``None`` on every boot, not only when the dashboard task hadn't yet
wired — the fields were structurally dead since they shipped.

v0.49.35 fix: drop the dead fields entirely. The ``dashboard_started``
event (`dashboard/server.py:1139-1143`) carries the live bind host +
port with the correct values; operators correlate via the startup
``saga_id`` + ``service_instance_id``.

This regression file pins:
1. ``_emit_network`` takes zero arguments (regression against
   re-introducing the ``config`` parameter solely to read the dead
   path).
2. The emitted ``startup.network`` payload contains the network
   observation fields (`hostname`, `fqdn`, `interface_count`,
   `interfaces`).
3. The emitted payload does NOT contain ``network.dashboard_host``
   or ``network.dashboard_port``.

Mission anchor: closure mission
``docs-internal/missions/MISSION-forensic-audit-closure-2026-05-20.md``
§0.3 (L3 was excluded by operator directive 2026-05-20; the operator
revoked the exclusion 2026-05-20 evening and authorised this fix).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import patch

from sovyx.observability import self_diagnosis


def _capture_emit_network() -> dict[str, Any]:
    """Invoke ``_emit_network`` and return the kwargs of its log call."""
    captured: dict[str, Any] = {}

    def _capture(event: str, **kwargs: Any) -> None:
        captured["event"] = event
        captured["fields"] = kwargs

    with patch.object(self_diagnosis.logger, "info", side_effect=_capture):
        asyncio.run(self_diagnosis._emit_network())  # noqa: SLF001

    return captured


def test_l3_emit_network_takes_no_arguments() -> None:
    """``_emit_network`` MUST NOT accept a ``config`` parameter.

    Regression against re-introducing the dead-path config read. The
    only purpose of the previous ``config`` parameter was to read
    ``config.dashboard.host/port``; with those fields removed, the
    parameter is dead weight + a footgun for future contributors who
    might re-introduce the wrong-attribute lookup.
    """
    signature = inspect.signature(self_diagnosis._emit_network)  # noqa: SLF001
    assert list(signature.parameters) == [], (
        f"_emit_network signature regressed to {signature}; expected ()."
    )


def test_l3_startup_network_carries_zero_dashboard_fields() -> None:
    """``startup.network`` payload MUST NOT contain dashboard bind fields.

    The forensic-audit §L3 closure removes the always-``None`` fields
    rather than redirecting the read to ``config.api.host/port``,
    because ``dashboard_started`` already carries that data with the
    correct values at the correct lifecycle moment.
    """
    captured = _capture_emit_network()
    assert captured["event"] == "startup.network"

    fields = captured["fields"]
    assert "network.dashboard_host" not in fields, (
        "regression: network.dashboard_host re-appeared in startup.network — "
        "FORENSIC §L3 closure (v0.49.35) deleted this dead field; the "
        "dashboard_started event is the canonical source of bind state."
    )
    assert "network.dashboard_port" not in fields, (
        "regression: network.dashboard_port re-appeared in startup.network — "
        "FORENSIC §L3 closure (v0.49.35) deleted this dead field; the "
        "dashboard_started event is the canonical source of bind state."
    )


def test_l3_startup_network_preserves_canonical_network_fields() -> None:
    """The closure removes ONLY the dead fields; canonical network state stays.

    Hostname, FQDN, interface count and the interfaces array remain
    the canonical network-observation surface emitted by the
    ``startup.network`` step of the diagnosis saga.
    """
    captured = _capture_emit_network()
    fields = captured["fields"]

    assert "network.hostname" in fields
    assert isinstance(fields["network.hostname"], str)
    assert "network.fqdn" in fields
    assert isinstance(fields["network.fqdn"], str)
    assert "network.interface_count" in fields
    assert isinstance(fields["network.interface_count"], int)
    assert fields["network.interface_count"] >= 0
    assert "network.interfaces" in fields
    assert isinstance(fields["network.interfaces"], list)
