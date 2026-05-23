"""Mission OX-1.B — Read-only causal join over the ``reason`` SSoT.

Composes three already-existing primitives — :class:`DegradedEntry`,
:class:`AckRecord`, and a caller-supplied ``anomaly_event_name → reason``
adapter map — into a single per-``reason`` join view. Pure function;
zero I/O; deterministic ordering; never raises on partial input.

Why ``reason`` (not a new ``incident_id``): per the Causal Modeling
Agent's Wave 2 analysis, ``reason`` is the only field already touching
all three pipelines:

* :meth:`EngineDegradedStore.record(... reason=...)`
* :meth:`OperatorAcksStore.record_ack(reason=...)`
* :class:`QuarantineReason(StrEnum)` SSoT
  (``src/sovyx/voice/health/_quarantine_reasons.py``)

The governor's HEALTHY-edge clear path at
:func:`_resource_cohort_governor._clear_axis_entry_for_reason` already
calls both :meth:`store.clear_reason(reason)` and (best-effort)
:meth:`OperatorAcksStore.clear_ack(reason)`, treating ``reason`` as
the existing 3-way join key (Mission B B-P0-3 closure 2026-05-21).

Adding a synthetic ``incident_id`` would force a write path on every
record/ack call (invasive). Mutating :class:`DegradedEntry` to add a
column is BLOCKED by the platform superstate frozen-file rule
(``PLATFORM-SUPERSTATE-2026-05-21.md`` §2). Heuristic timestamp+axis
windowing violates anti-pattern #52 (comment-vs-code parity).
``reason`` is the cheapest correct answer.

Anomaly events emit ``event_name`` (e.g.
``"anomaly.latency_spike"``); they carry no ``reason`` field today.
This helper accepts an optional ``anomaly_reason_map`` —
``{event_name: reason}`` — so a caller (CLI explain mode in OX-1.D,
say) can declare which anomalies are sibling to which degraded reason.
Producing the map is out of scope for OX-1.B; the helper merely
threads the structural slot through.

Discipline:

* Pure function; no log emission; no async I/O; no mutation of
  inputs.
* Read-only over already-snapshotted state — callers obtain
  ``degraded_snapshot`` via :meth:`EngineDegradedStore.snapshot()`
  and ``active_acks`` via :meth:`OperatorAcksStore.list_active_acks()`.
  Snapshot freshness is the caller's responsibility.
* Deterministic ordering — sorted by ``reason`` so consumers can
  diff successive views without spurious churn.
* Reason-keyed dedupe — never collapses two distinct reasons into
  one entry. Always one ``CausalJoinEntry`` per distinct reason
  observed across all three input streams.
* No PLATFORM-SUPERSTATE §2 frozen-file mutation. Both
  :class:`DegradedEntry` and :class:`AckRecord` are consumed as
  frozen dataclasses; no fields added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sovyx.engine._degraded_store import DegradedEntry
    from sovyx.engine._operator_acks_store import AckRecord


@dataclass(frozen=True, slots=True)
class CausalJoinEntry:
    """One reason-keyed join row across the three pipelines.

    Attributes:
        reason: The SSoT join key (matches
            :attr:`DegradedEntry.reason` and :attr:`AckRecord.reason`).
        degraded: The live :class:`DegradedEntry` for this reason, or
            ``None`` when no degraded entry exists (e.g. the
            governor's HEALTHY-edge clear has fired but the operator
            ack TTL has not yet expired — ack-only row).
        ack: The live :class:`AckRecord` for this reason, or ``None``
            when no operator has acked (e.g. fresh breach not yet
            seen by an operator).
        anomaly_event_names: Tuple of anomaly event names the caller
            has declared sibling to this reason via the
            ``anomaly_reason_map`` argument. Empty tuple by default.
    """

    reason: str
    degraded: DegradedEntry | None = None
    ack: AckRecord | None = None
    anomaly_event_names: tuple[str, ...] = ()

    @property
    def has_degraded(self) -> bool:
        return self.degraded is not None

    @property
    def has_ack(self) -> bool:
        return self.ack is not None

    @property
    def has_anomaly_correlation(self) -> bool:
        return bool(self.anomaly_event_names)


def build_causal_join_view(
    degraded_snapshot: Iterable[DegradedEntry],
    active_acks: Iterable[AckRecord],
    *,
    anomaly_reason_map: Mapping[str, str] | None = None,
) -> list[CausalJoinEntry]:
    """Compose a per-``reason`` join view over degraded + ack + anomaly streams.

    Pure; deterministic; reason-keyed; alphabetically sorted by
    ``reason`` for diff-stable consumer output.

    Args:
        degraded_snapshot: Result of
            :meth:`EngineDegradedStore.snapshot()`. Multiple entries
            sharing the same ``reason`` MUST NOT occur per the store's
            ``_by_reason`` invariant; if they do, the LAST occurrence
            wins (matches dict semantics).
        active_acks: Result of
            :meth:`OperatorAcksStore.list_active_acks()`. Same
            dedupe-on-collision rule as above.
        anomaly_reason_map: Optional mapping from anomaly
            ``event_name`` (e.g. ``"anomaly.latency_spike"``) to the
            ``reason`` token it correlates to. When provided, the
            join's ``anomaly_event_names`` field surfaces the set of
            event names sibling to each reason. Pass ``None`` to omit
            the anomaly correlation entirely.

    Returns:
        Sorted list of :class:`CausalJoinEntry`. Always one entry per
        distinct reason observed across the three inputs. Never
        empty unless all three inputs are empty.
    """
    by_reason: dict[str, dict[str, object]] = {}

    for entry in degraded_snapshot:
        by_reason.setdefault(entry.reason, {})["degraded"] = entry

    for ack in active_acks:
        by_reason.setdefault(ack.reason, {})["ack"] = ack

    if anomaly_reason_map:
        # Group anomaly event names by reason. Per-reason ordering is
        # alphabetical for diff stability; the original mapping order
        # is intentionally NOT preserved (preserving it would couple
        # caller dict-construction order to the join view's render
        # order, which is a fragile contract).
        anomaly_by_reason: dict[str, list[str]] = {}
        for event_name, reason in anomaly_reason_map.items():
            anomaly_by_reason.setdefault(reason, []).append(event_name)
        for reason, event_names in anomaly_by_reason.items():
            by_reason.setdefault(reason, {})["anomaly_event_names"] = tuple(sorted(event_names))

    return [
        CausalJoinEntry(
            reason=reason,
            degraded=fields.get("degraded"),  # type: ignore[arg-type]
            ack=fields.get("ack"),  # type: ignore[arg-type]
            anomaly_event_names=fields.get("anomaly_event_names", ()),  # type: ignore[arg-type]
        )
        for reason, fields in sorted(by_reason.items())
    ]


__all__ = ["CausalJoinEntry", "build_causal_join_view"]
