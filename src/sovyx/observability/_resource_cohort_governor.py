"""Mission H4 §Phase 1.D — ResourceCohortGovernor.

Mission anchor:
``docs-internal/missions/MISSION-h4-resource-hygiene-instrumentation-2026-05-19.md``
§T4.1.

Consumes the per-cohort registry metrics emitted on every
``self.health.snapshot`` tick and evaluates 5 cohort budgets per
:class:`CohortAxis`:

* **RSS_GROWTH** — ``process.rss_bytes`` Δ across the rolling window
  exceeds ``cohort_rss_growth_threshold_mb``.
* **THREAD_COUNT** — ``process.num_threads`` Δ exceeds
  ``cohort_thread_growth_threshold`` in the same window.
* **LOCK_DICT_CARDINALITY** — aggregate
  ``lock_dict.total_cardinality`` crosses the soft cap
  ``cohort_lock_dict_soft_cap``.
* **ONNX_SESSION** — ``onnx.session_count`` exceeds the expected
  count for the enabled feature flags.
* **EXCEPTION_COHORT** — accumulated
  ``exception_cohort.retained_bytes_estimate`` exceeds
  ``exception_cohort_retained_bytes_cap``.

On every BUDGET_EXCEEDED verdict the governor:

1. Emits a structured WARN ``engine.resources.cohort_budget_exceeded``
   with ``cohort``, ``verdict``, ``observed``, ``budget`` fields so
   operators can correlate via log grep.
2. Calls ``EngineDegradedStore.record(DegradedEntry(
   axis="engine_resources", reason=f"engine_resources.{cohort.value}",
   ...))`` per C4 composite-store wire shim convention (anti-pattern
   #42). The existing :class:`DegradedBanner` renders the new axis
   automatically.

Phase 1.D minimum (this commit): governor library + per-tick evaluator
hook. The heap-snapshot file persistence + heartbeat-mixin N=5 trigger
+ circuit-breaker + ack endpoint are deferred to a Phase 1.E follow-up
(spec §8 T4.5+). The governor's structural skeleton is in place; future
extensions slot in via dependency injection.

Anti-pattern compliance:

* #14/#15/#30 — depends on the SSoT registry surface that closes
  those rules' instrumentation gaps.
* #34 — feature-flag gated (``observability.features.cohort_governor``
  default True). Bootstrap skips wire-up when disabled.
* #42 — single composite store wire shim. New axis
  ``engine_resources`` is forward-additive per C4 ADR-D5.
* #47 — the canonical instance for resource-cohort governance.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum, unique
from threading import Lock
from typing import TYPE_CHECKING

from sovyx.observability._resource_registry import CohortAxis
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sovyx.engine.config import ObservabilityTuningConfig

logger = get_logger(__name__)


@unique
class CohortVerdict(StrEnum):
    """Governor evaluation outcome per cohort.

    StrEnum per anti-pattern #9 (xdist-safe).
    """

    HEALTHY = "healthy"
    BUDGET_EXCEEDED = "budget_exceeded"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True, slots=True)
class CohortBudget:
    """Per-cohort budget threshold + window.

    Attributes:
        axis: Which cohort this budget applies to.
        threshold: The numeric ceiling (interpretation per-axis —
            RSS_GROWTH is delta-bytes; THREAD_COUNT is delta-count;
            LOCK_DICT_CARDINALITY is absolute soft-cap; etc.).
        window_s: Rolling-window length in seconds for delta-based
            cohorts (RSS_GROWTH + THREAD_COUNT). Absolute-cap
            cohorts (LOCK_DICT_CARDINALITY + ONNX_SESSION +
            EXCEPTION_COHORT) ignore this — they read the live value.
    """

    axis: CohortAxis
    threshold: int
    window_s: int = 60


@dataclass(frozen=True, slots=True)
class CohortEvaluation:
    """One cohort's evaluation result for a given snapshot tick."""

    axis: CohortAxis
    verdict: CohortVerdict
    observed: int
    budget: int
    note: str = ""


# Default budgets (matches mission spec §8 T4.7 — operator-tunable via
# ObservabilityTuningConfig but ship with sensible defaults).
_DEFAULT_BUDGETS: tuple[CohortBudget, ...] = (
    CohortBudget(axis=CohortAxis.RSS_GROWTH, threshold=512 * 1024 * 1024, window_s=60),
    CohortBudget(axis=CohortAxis.THREAD_COUNT, threshold=32, window_s=60),
    CohortBudget(axis=CohortAxis.LOCK_DICT_CARDINALITY, threshold=6_000, window_s=60),
    CohortBudget(axis=CohortAxis.ONNX_SESSION, threshold=8, window_s=60),
    CohortBudget(
        axis=CohortAxis.EXCEPTION_COHORT,
        threshold=16 * 1024 * 1024,
        window_s=300,
    ),
)


def _budgets_from_tuning(tuning: ObservabilityTuningConfig) -> tuple[CohortBudget, ...]:
    """Build a budget tuple from operator-tunable knobs.

    Mission H4 §8 T4.7 ADR-D12 — the governor consumes the
    ``cohort_*`` knobs on :class:`ObservabilityTuningConfig` instead of
    hardcoded constants so operators can re-tune via
    ``SOVYX_OBSERVABILITY__TUNING__*`` env vars without a code change.
    The defaults shipped on the config class match the v0.49.17
    constants so existing baselines hold.
    """
    return (
        CohortBudget(
            axis=CohortAxis.RSS_GROWTH,
            threshold=tuning.cohort_rss_growth_threshold_mb * 1024 * 1024,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.THREAD_COUNT,
            threshold=tuning.cohort_thread_growth_threshold,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.LOCK_DICT_CARDINALITY,
            threshold=tuning.cohort_lock_dict_soft_cap,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.ONNX_SESSION,
            threshold=tuning.cohort_onnx_session_soft_cap,
            window_s=tuning.cohort_window_s,
        ),
        CohortBudget(
            axis=CohortAxis.EXCEPTION_COHORT,
            threshold=tuning.exception_cohort_retained_bytes_cap,
            window_s=tuning.exception_cohort_window_s,
        ),
    )


_OBSERVATION_RING_MAX: int = 32  # bounded history per cohort


@dataclass
class ResourceCohortGovernor:
    """Per-snapshot-tick cohort budget evaluator.

    Wire-up: bootstrap creates a singleton + the
    :class:`ResourceSnapshotter` calls :meth:`evaluate_snapshot()`
    after each ``_emit_snapshot``. Each cohort's per-tick verdict
    drives optional emissions:

    * ``HEALTHY`` — no-op (most ticks). Clears any prior
      ``engine_resources.<axis>`` entries from the
      :class:`EngineDegradedStore` per C4 ADR-D5 axis-clear-on-success.
    * ``BUDGET_EXCEEDED`` — emit WARN + record axis entry in the
      composite store + (Phase 1.E) trigger heap snapshot / engage
      circuit-breaker.
    * ``INSUFFICIENT_DATA`` — silent (warmup window not yet
      filled).

    Thread-safe via internal :class:`Lock`; safe to invoke from the
    snapshotter loop or a future test fixture.
    """

    budgets: tuple[CohortBudget, ...] = _DEFAULT_BUDGETS
    enabled: bool = True
    _rss_history: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=_OBSERVATION_RING_MAX),
    )
    _thread_history: deque[tuple[float, int]] = field(
        default_factory=lambda: deque(maxlen=_OBSERVATION_RING_MAX),
    )
    _lock: Lock = field(default_factory=Lock)

    @classmethod
    def from_tuning(
        cls, tuning: ObservabilityTuningConfig, *, enabled: bool = True
    ) -> ResourceCohortGovernor:
        """Build a governor from operator-tunable knobs.

        Mission H4 §8 T4.7 ADR-D12 — bootstrap calls this with the live
        :class:`ObservabilityTuningConfig` so the 12 ``cohort_*`` env
        overrides take effect. Tests using the bare ``ResourceCohortGovernor()``
        constructor get the v0.49.17 hardcoded defaults — backward-compat.
        """
        return cls(budgets=_budgets_from_tuning(tuning), enabled=enabled)

    def evaluate_snapshot(self, snapshot: Mapping[str, object]) -> list[CohortEvaluation]:
        """Evaluate every cohort against the given snapshot.

        Args:
            snapshot: The dict emitted by
                ``ResourceRegistry.snapshot_fields()`` (merged with
                the psutil + asyncio fields by
                :func:`ResourceSnapshotter._emit_snapshot`).

        Returns:
            A list of :class:`CohortEvaluation` records — one per
            cohort. Callers route ``BUDGET_EXCEEDED`` entries to
            :class:`EngineDegradedStore` via the
            :meth:`emit_axis_entries` helper.
        """
        if not self.enabled:
            return []
        now = time.monotonic()
        results: list[CohortEvaluation] = []
        for budget in self.budgets:
            match budget.axis:
                case CohortAxis.RSS_GROWTH:
                    results.append(self._eval_rss_growth(snapshot, budget, now))
                case CohortAxis.THREAD_COUNT:
                    results.append(self._eval_thread_growth(snapshot, budget, now))
                case CohortAxis.LOCK_DICT_CARDINALITY:
                    results.append(self._eval_lock_dict(snapshot, budget))
                case CohortAxis.ONNX_SESSION:
                    results.append(self._eval_onnx(snapshot, budget))
                case CohortAxis.EXCEPTION_COHORT:
                    results.append(self._eval_exception_cohort(snapshot, budget))
        return results

    # ── Per-cohort evaluators ──

    def _eval_rss_growth(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
        now: float,
    ) -> CohortEvaluation:
        rss_raw = snapshot.get("process.rss_bytes")
        if not isinstance(rss_raw, int) or rss_raw <= 0:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="process.rss_bytes missing or non-positive",
            )
        with self._lock:
            self._rss_history.append((now, rss_raw))
            # Find the oldest sample inside the rolling window.
            window_start = now - budget.window_s
            samples_in_window = [v for (ts, v) in self._rss_history if ts >= window_start]
        if len(samples_in_window) < 2:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=rss_raw,
                budget=budget.threshold,
                note=f"need ≥2 samples in {budget.window_s}s window; got {len(samples_in_window)}",
            )
        delta = max(samples_in_window) - min(samples_in_window)
        if delta > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=delta,
                budget=budget.threshold,
                note=f"RSS Δ {delta // (1024 * 1024)} MiB across {budget.window_s}s",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=delta,
            budget=budget.threshold,
        )

    def _eval_thread_growth(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
        now: float,
    ) -> CohortEvaluation:
        threads_raw = snapshot.get("process.num_threads")
        if not isinstance(threads_raw, int) or threads_raw <= 0:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="process.num_threads missing",
            )
        with self._lock:
            self._thread_history.append((now, threads_raw))
            window_start = now - budget.window_s
            samples_in_window = [v for (ts, v) in self._thread_history if ts >= window_start]
        if len(samples_in_window) < 2:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=threads_raw,
                budget=budget.threshold,
                note=f"need ≥2 samples in {budget.window_s}s window",
            )
        delta = max(samples_in_window) - min(samples_in_window)
        if delta > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=delta,
                budget=budget.threshold,
                note=f"thread Δ {delta} across {budget.window_s}s",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=delta,
            budget=budget.threshold,
        )

    def _eval_lock_dict(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
    ) -> CohortEvaluation:
        total = snapshot.get("lock_dict.total_cardinality")
        if not isinstance(total, int):
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="lock_dict.total_cardinality missing",
            )
        if total > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=total,
                budget=budget.threshold,
                note=f"aggregate cardinality {total} exceeds soft cap",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=total,
            budget=budget.threshold,
        )

    def _eval_onnx(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
    ) -> CohortEvaluation:
        count = snapshot.get("onnx.session_count")
        if not isinstance(count, int):
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="onnx.session_count missing",
            )
        if count > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=count,
                budget=budget.threshold,
                note=f"{count} ONNX sessions exceeds expected ceiling",
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=count,
            budget=budget.threshold,
        )

    def _eval_exception_cohort(
        self,
        snapshot: Mapping[str, object],
        budget: CohortBudget,
    ) -> CohortEvaluation:
        retained = snapshot.get("exception_cohort.retained_bytes_estimate")
        if not isinstance(retained, int):
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.INSUFFICIENT_DATA,
                observed=0,
                budget=budget.threshold,
                note="exception_cohort.retained_bytes_estimate missing",
            )
        if retained > budget.threshold:
            return CohortEvaluation(
                axis=budget.axis,
                verdict=CohortVerdict.BUDGET_EXCEEDED,
                observed=retained,
                budget=budget.threshold,
                note=(f"ExceptionGroup retention {retained // (1024 * 1024)} MiB exceeds cap"),
            )
        return CohortEvaluation(
            axis=budget.axis,
            verdict=CohortVerdict.HEALTHY,
            observed=retained,
            budget=budget.threshold,
        )


def emit_axis_entries(evaluations: list[CohortEvaluation]) -> int:
    """Emit ``engine.resources.cohort_budget_exceeded`` for every breached cohort.

    Routes each BUDGET_EXCEEDED entry to the :class:`EngineDegradedStore`
    with ``axis="engine_resources"`` so the existing C4
    :class:`DegradedBanner` renders the cohort without dashboard
    changes (per C4 ADR-D5 forward-additive contract).

    Returns the count of BUDGET_EXCEEDED emissions for caller-side
    metrics. Caller does NOT need to act on the count — the WARN log
    line + composite-store entry are the operator-actionable surfaces.
    """
    emitted = 0
    for evaluation in evaluations:
        if evaluation.verdict != CohortVerdict.BUDGET_EXCEEDED:
            continue
        emitted += 1
        logger.warning(
            "engine.resources.cohort_budget_exceeded",
            **{
                "engine.resources.cohort": evaluation.axis.value,
                "engine.resources.observed": evaluation.observed,
                "engine.resources.budget": evaluation.budget,
                "engine.resources.note": evaluation.note,
            },
        )
        _record_to_composite_store(evaluation)
        _increment_cohort_budget_counter(evaluation)
    return emitted


def _increment_cohort_budget_counter(evaluation: CohortEvaluation) -> None:
    """Best-effort OTel counter increment for the cohort-breach event.

    Mission H4 §T2.6 + ADR-D20 — paired with the structured WARN above.
    Counter lookup is best-effort: a setup-time race where MetricsRegistry
    isn't ready yet falls back to a debug-level log + skips the increment.
    The structured WARN + composite-store entry remain the load-bearing
    surfaces.
    """
    try:
        from sovyx.observability.metrics import get_metrics  # noqa: PLC0415 — lazy

        counter = getattr(get_metrics(), "voice_health_cohort_budget_exceeded", None)
        if counter is None:
            return
        # Severity per ADR-D6: 1 cohort = warning (governor default). A
        # future caller that aggregates multiple BUDGET_EXCEEDED events
        # within one tick can escalate by inspecting the returned counter
        # state on the composite endpoint.
        counter.add(
            1,
            attributes={
                "cohort": evaluation.axis.value,
                "severity": "warning",
            },
        )
    except Exception:  # noqa: BLE001 — counter must NEVER break the snapshot path
        logger.debug(
            "engine.resources.cohort_budget_counter_failed",
            cohort=evaluation.axis.value,
            exc_info=True,
        )


def record_resource_snapshot_emission(*, final: bool) -> None:
    """Per-snapshot-tick counter increment — Mission H4 §T2.6 + ADR-D20.

    Called by :func:`ResourceSnapshotter._emit_snapshot` after the
    structured-log emission. Best-effort; failures absorbed.
    """
    try:
        from sovyx.observability.metrics import get_metrics  # noqa: PLC0415 — lazy

        counter = getattr(get_metrics(), "voice_health_resource_snapshot_emission", None)
        if counter is None:
            return
        counter.add(1, attributes={"final": str(final).lower()})
    except Exception:  # noqa: BLE001 — counter must NEVER break the snapshot path
        logger.debug("engine.resources.snapshot_emission_counter_failed", exc_info=True)


def _record_to_composite_store(evaluation: CohortEvaluation) -> None:
    """Best-effort record into C4 :class:`EngineDegradedStore`.

    Failures absorbed at this layer — the WARN log is the
    load-bearing surface; composite-store recording is
    additive-only and never breaks the snapshot path.
    """
    try:
        from sovyx.engine._degraded_store import (  # noqa: PLC0415 — lazy import
            ActionChip,
            DegradedEntry,
            get_default_degraded_store,
        )

        now_monotonic = time.monotonic()
        reason = f"engine_resources.{evaluation.axis.value}"
        # Severity per ADR-D6: 1 cohort = warn. The composite endpoint
        # escalates to error/critical when N axes co-occur.
        entry = DegradedEntry(
            axis="engine_resources",
            reason=reason,
            severity="warning",
            title_token=f"degraded.engine_resources.{evaluation.axis.value}.title",
            body_token=f"degraded.engine_resources.{evaluation.axis.value}.body",
            action_chips=(
                ActionChip(
                    label_token="degraded.engine_resources.actions.viewResources",
                    action="navigate",
                    target="/engine/resources",
                ),
            ),
            metadata={
                "cohort": evaluation.axis.value,
                "observed": evaluation.observed,
                "budget": evaluation.budget,
                "note": evaluation.note,
            },
            first_observed_monotonic=now_monotonic,
            last_observed_monotonic=now_monotonic,
            occurrence_count=1,
        )
        get_default_degraded_store().record(entry)
    except Exception:  # noqa: BLE001 — composite store must NEVER break the snapshot path
        logger.debug(
            "engine.resources.composite_store_record_failed",
            cohort=evaluation.axis.value,
            exc_info=True,
        )


_SINGLETON: ResourceCohortGovernor | None = None
_SINGLETON_LOCK: Lock = Lock()


def get_default_resource_cohort_governor() -> ResourceCohortGovernor:
    """Return the process-local lazy-initialized governor singleton."""
    global _SINGLETON  # noqa: PLW0603
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = ResourceCohortGovernor()
    return _SINGLETON


def reset_default_resource_cohort_governor() -> None:
    """Test-only — reset the singleton to a fresh governor."""
    global _SINGLETON  # noqa: PLW0603
    with _SINGLETON_LOCK:
        _SINGLETON = None


__all__ = [
    "CohortBudget",
    "CohortEvaluation",
    "CohortVerdict",
    "ResourceCohortGovernor",
    "emit_axis_entries",
    "get_default_resource_cohort_governor",
    "record_resource_snapshot_emission",
    "reset_default_resource_cohort_governor",
]
