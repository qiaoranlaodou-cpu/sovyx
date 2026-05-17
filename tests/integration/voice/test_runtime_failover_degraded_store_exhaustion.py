"""Integration test — runtime failover's ladder-exhausted wire shim
populates EngineDegradedStore + clears on subsequent success.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.4 + §9.2.

The Phase 1.A wire shim at ``voice/health/_runtime_failover.py`` ALSO
records to the store when the failover ladder exhausts, AND clears
the voice axis when a subsequent ladder run succeeds. This test
exercises both paths by directly invoking the record/clear contract
the runtime_failover loop uses, asserting:

1. Exhausted path lands axis=voice + reason=failover_ladder_exhausted.
2. Success path clears the voice axis (banner stops surfacing).
3. The candidates_unreachable list is captured verbatim in metadata.

Avoids exercising the full failover ladder (covered by §9.4 regression
test test_c4_decorative_daemon_replay + the C3 ladder iteration
tests); this test pins the C4 wire shim's record/clear contract.
"""

from __future__ import annotations

import time

import pytest

from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    make_action_chip,
    reset_default_degraded_store,
)


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


def _runtime_failover_exhausted_record(
    candidates_unreachable: list[str],
    ladder_id: str = "test_ladder_001",
    mind_id: str = "default",
) -> None:
    """Mirror the exhausted-path record at _runtime_failover.py."""
    _now = time.monotonic()
    get_default_degraded_store().record(
        DegradedEntry(
            axis="voice",
            reason="failover_ladder_exhausted",
            severity="error",
            title_token="degraded.voice.ladderExhausted.title",
            body_token="degraded.voice.ladderExhausted.body",
            action_chips=(
                make_action_chip(
                    "degraded.voice.ladderExhausted.viewHistory",
                    "navigate",
                    "/voice/health",
                    style="primary",
                ),
                make_action_chip(
                    "degraded.voice.ladderExhausted.reconnectUsb",
                    "external_link",
                    "https://sovyx.dev/docs/voice/troubleshooting",
                ),
            ),
            metadata={
                "candidates_unreachable": list(candidates_unreachable),
                "candidates_tried": len(candidates_unreachable),
                "ladder_id": ladder_id,
                "mind_id": mind_id,
            },
            first_observed_monotonic=_now,
            last_observed_monotonic=_now,
            occurrence_count=1,
        ),
    )


def _runtime_failover_succeeded_clear() -> int:
    """Mirror the success-path clear_axis call at _runtime_failover.py."""
    return get_default_degraded_store().clear_axis("voice")


class TestRuntimeFailoverDegradedStoreExhaustion:
    def test_exhausted_path_lands_axis_voice(self) -> None:
        _runtime_failover_exhausted_record(["razer-usb", "pipewire-default"])
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        assert entries[0].axis == "voice"
        assert entries[0].reason == "failover_ladder_exhausted"
        assert entries[0].severity == "error"

    def test_candidates_unreachable_captured_verbatim(self) -> None:
        candidates = ["razer-usb-1532", "pipewire-default-idx7", "os-default-idx8"]
        _runtime_failover_exhausted_record(candidates)
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.metadata["candidates_unreachable"] == candidates
        assert entry.metadata["candidates_tried"] == 3

    def test_success_path_clears_voice_axis(self) -> None:
        _runtime_failover_exhausted_record(["a", "b"])
        assert len(get_default_degraded_store()) == 1
        removed = _runtime_failover_succeeded_clear()
        assert removed == 1
        assert len(get_default_degraded_store()) == 0

    def test_clear_voice_axis_preserves_other_axes(self) -> None:
        """The success-path MUST clear ONLY the voice axis, not the
        whole store. Operator's LLM + STT axes (if degraded) must
        survive a voice ladder success."""
        store = get_default_degraded_store()
        # Pre-seed LLM + STT axes.
        store.record(
            DegradedEntry(
                axis="llm",
                reason="no_llm_provider",
                severity="error",
                title_token="x",
                body_token="y",
                action_chips=(),
                metadata={},
                first_observed_monotonic=time.monotonic(),
                last_observed_monotonic=time.monotonic(),
                occurrence_count=1,
            ),
        )
        store.record(
            DegradedEntry(
                axis="stt",
                reason="stt_language_coerced",
                severity="warn",
                title_token="x",
                body_token="y",
                action_chips=(),
                metadata={},
                first_observed_monotonic=time.monotonic(),
                last_observed_monotonic=time.monotonic(),
                occurrence_count=1,
            ),
        )
        # Add voice axis (ladder exhausted).
        _runtime_failover_exhausted_record(["a"])
        assert len(store) == 3

        # Clear voice (success).
        removed = _runtime_failover_succeeded_clear()
        assert removed == 1

        # LLM + STT survive.
        axes = {e.axis for e in store.snapshot()}
        assert axes == {"llm", "stt"}

    def test_ladder_id_in_metadata_for_log_correlation(self) -> None:
        _runtime_failover_exhausted_record(["a"], ladder_id="abc123def456")
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.metadata["ladder_id"] == "abc123def456"

    def test_mind_id_in_metadata(self) -> None:
        _runtime_failover_exhausted_record(
            ["a"],
            ladder_id="x",
            mind_id="jonny",
        )
        entry = get_default_degraded_store().snapshot()[0]
        assert entry.metadata["mind_id"] == "jonny"
