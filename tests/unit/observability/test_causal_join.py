"""Mission OX-1.B — unit tests for ``observability/_causal_join.py``.

Contract:

* Pure function; deterministic ordering (alphabetical by ``reason``).
* One :class:`CausalJoinEntry` per distinct reason observed across the
  three input streams. Never collapses two reasons into one row.
* Always returns a list (never raises); empty inputs → empty list.
* Anomaly map is optional; when omitted, ``anomaly_event_names`` is
  the empty tuple on every entry.
* ``has_degraded`` / ``has_ack`` / ``has_anomaly_correlation``
  helper properties reflect presence accurately.

xdist-safe per anti-pattern #8 — no isinstance against private
classes; structural comparison only.
"""

from __future__ import annotations

import time

import pytest

from sovyx.engine._degraded_store import DegradedEntry
from sovyx.engine._operator_acks_store import AckRecord
from sovyx.observability._causal_join import (
    build_causal_join_view,
)


def _degraded(reason: str, axis: str = "engine_resources") -> DegradedEntry:
    return DegradedEntry(
        axis=axis,
        reason=reason,
        severity="warn",
        title_token=f"{reason}.title",
        body_token=f"{reason}.body",
    )


def _ack(reason: str, ttl_sec: int = 3600) -> AckRecord:
    return AckRecord(
        reason=reason,
        acked_at_ts=int(time.time()),
        ttl_sec=ttl_sec,
        operator_id="test-operator",
    )


class TestBuildCausalJoinView:
    """Contract tests for the read-only join helper."""

    def test_empty_inputs_return_empty_list(self) -> None:
        result = build_causal_join_view([], [])
        assert result == []

    def test_single_degraded_no_ack_no_anomaly(self) -> None:
        entry = _degraded("engine_resources.rss_growth")
        result = build_causal_join_view([entry], [])
        assert len(result) == 1
        assert result[0].reason == "engine_resources.rss_growth"
        assert result[0].degraded is entry
        assert result[0].ack is None
        assert result[0].anomaly_event_names == ()
        assert result[0].has_degraded is True
        assert result[0].has_ack is False
        assert result[0].has_anomaly_correlation is False

    def test_single_ack_no_degraded(self) -> None:
        """Ack-only row: the governor cleared the degraded entry but
        the operator's ack TTL hasn't expired yet — operator still
        sees an ack but no live degraded entry."""
        ack = _ack("engine_resources.rss_growth")
        result = build_causal_join_view([], [ack])
        assert len(result) == 1
        assert result[0].degraded is None
        assert result[0].ack is ack
        assert result[0].has_degraded is False
        assert result[0].has_ack is True

    def test_paired_degraded_and_ack_share_one_entry(self) -> None:
        """The whole point of the join: degraded + ack with same
        ``reason`` collapse into one ``CausalJoinEntry``, NOT two."""
        reason = "engine_resources.thread_count"
        degraded = _degraded(reason)
        ack = _ack(reason)
        result = build_causal_join_view([degraded], [ack])
        assert len(result) == 1
        assert result[0].reason == reason
        assert result[0].degraded is degraded
        assert result[0].ack is ack

    def test_distinct_reasons_never_collapse(self) -> None:
        """Two reasons → two entries. Reason-keyed dedupe must NEVER
        merge distinct reasons."""
        d1 = _degraded("engine_resources.rss_growth")
        d2 = _degraded("engine_resources.thread_count")
        result = build_causal_join_view([d1, d2], [])
        assert len(result) == 2
        reasons = {e.reason for e in result}
        assert reasons == {
            "engine_resources.rss_growth",
            "engine_resources.thread_count",
        }

    def test_deterministic_alphabetical_ordering(self) -> None:
        """Output is sorted by ``reason`` so consumers can diff
        successive snapshots without spurious churn (per docstring
        contract)."""
        d_z = _degraded("zeta.reason")
        d_a = _degraded("alpha.reason")
        d_m = _degraded("mu.reason")
        result = build_causal_join_view([d_z, d_a, d_m], [])
        reasons = [e.reason for e in result]
        assert reasons == sorted(reasons)
        assert reasons == ["alpha.reason", "mu.reason", "zeta.reason"]

    def test_anomaly_map_enriches_matching_reason(self) -> None:
        """When anomaly_reason_map is provided, the join's
        ``anomaly_event_names`` field surfaces sibling event names."""
        d = _degraded("engine_resources.rss_growth")
        result = build_causal_join_view(
            [d],
            [],
            anomaly_reason_map={
                "anomaly.memory_growth": "engine_resources.rss_growth",
            },
        )
        assert len(result) == 1
        assert result[0].anomaly_event_names == ("anomaly.memory_growth",)
        assert result[0].has_anomaly_correlation is True

    def test_anomaly_map_creates_anomaly_only_row(self) -> None:
        """An anomaly with no matching degraded/ack still produces a
        row — operator can see ``anomaly fired but no degraded entry
        recorded`` (the very ``three independent pipelines`` gap the
        OX-1 audit surfaced)."""
        result = build_causal_join_view(
            [],
            [],
            anomaly_reason_map={
                "anomaly.latency_spike": "synthetic.latency_reason",
            },
        )
        assert len(result) == 1
        assert result[0].reason == "synthetic.latency_reason"
        assert result[0].degraded is None
        assert result[0].ack is None
        assert result[0].anomaly_event_names == ("anomaly.latency_spike",)

    def test_multiple_anomalies_per_reason_sorted_alphabetically(self) -> None:
        """Per docstring: per-reason anomaly event names are
        alphabetically sorted for diff stability — NOT in caller dict
        order."""
        result = build_causal_join_view(
            [],
            [],
            anomaly_reason_map={
                "anomaly.zeta_spike": "shared.reason",
                "anomaly.alpha_spike": "shared.reason",
                "anomaly.mu_spike": "shared.reason",
            },
        )
        assert len(result) == 1
        assert result[0].anomaly_event_names == (
            "anomaly.alpha_spike",
            "anomaly.mu_spike",
            "anomaly.zeta_spike",
        )

    def test_three_way_join_when_all_three_present(self) -> None:
        """Full three-way join: same reason has degraded + ack +
        anomaly correlation."""
        reason = "engine_resources.rss_growth"
        d = _degraded(reason)
        a = _ack(reason)
        result = build_causal_join_view(
            [d],
            [a],
            anomaly_reason_map={"anomaly.memory_growth": reason},
        )
        assert len(result) == 1
        e = result[0]
        assert e.degraded is d
        assert e.ack is a
        assert e.anomaly_event_names == ("anomaly.memory_growth",)
        assert e.has_degraded and e.has_ack and e.has_anomaly_correlation

    def test_returned_dataclass_is_frozen(self) -> None:
        """``CausalJoinEntry`` is frozen — consumers MUST NOT mutate."""
        d = _degraded("engine_resources.rss_growth")
        result = build_causal_join_view([d], [])
        entry = result[0]
        with pytest.raises(Exception) as exc_info:
            entry.reason = "mutated"  # type: ignore[misc]
        # xdist-safe identity check per anti-pattern #8.
        assert type(exc_info.value).__name__ in {
            "FrozenInstanceError",
            "AttributeError",
        }

    def test_does_not_mutate_inputs(self) -> None:
        """Helper is pure. Input snapshots come back unchanged."""
        degraded_list = [_degraded("r1"), _degraded("r2")]
        ack_list = [_ack("r2")]
        original_degraded = list(degraded_list)
        original_acks = list(ack_list)
        build_causal_join_view(degraded_list, ack_list)
        assert degraded_list == original_degraded
        assert ack_list == original_acks
