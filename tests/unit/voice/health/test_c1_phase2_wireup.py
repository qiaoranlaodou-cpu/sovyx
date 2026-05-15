"""Mission C1 commit 5 — Phase 2 wire-up tests.

Covers §6 (Phase 2 — wire-up) + §20.M sub-task coverage from
``docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md``:

* :class:`TestRuntimeFailoverQuarantineReasons` — T2.1 / T2.1.b: the
  failover helper reads ``derived_reason`` from the active endpoint's
  :class:`QuarantineEntry` and surfaces both legacy + derived reasons
  on every telemetry event (split-by-class enables dashboard
  segmentation without per-event re-derivation).
* :class:`TestKernelInvalidatedRecheckEligibility` — T2.1.a §20.H:
  the kernel-rechecker filters entries through
  :func:`is_recheck_eligible` so VAD-frontend / format-mismatch
  quarantines (which recover via reset ladder BEFORE quarantine and
  are terminal-for-this-boot post-quarantine) are NOT re-probed.
* :class:`TestWatchdogApoClassFilter` — T2.1.a §20.H: the watchdog's
  APO-recheck loop uses :func:`is_apo_class_reason` so future
  APO-class extensions land in the loop without per-call-site edits.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sovyx.voice.health._quarantine import EndpointQuarantine
from sovyx.voice.health._runtime_failover import _snapshot_quarantine_reasons


class TestSnapshotQuarantineReasons:
    """T2.1 — read derived + legacy reasons from the live quarantine."""

    def test_empty_guid_returns_empty_strings(self) -> None:
        legacy, derived = _snapshot_quarantine_reasons("")
        assert (legacy, derived) == ("", "")

    def test_missing_entry_returns_empty_strings(self) -> None:
        """An unknown GUID yields empty strings — the helper degrades
        gracefully so the failover telemetry stays informative even
        when the quarantine store has expired the entry between
        quarantine and failover dispatch."""
        empty_q = EndpointQuarantine(quarantine_s=60.0)
        with patch(
            "sovyx.voice.health._quarantine.get_default_quarantine",
            return_value=empty_q,
        ):
            legacy, derived = _snapshot_quarantine_reasons("guid-missing")
        assert legacy == ""
        assert derived == ""

    def test_legacy_only_entry_mirrors_to_derived(self) -> None:
        """Pre-mission entries have empty ``derived_reason`` — the
        helper falls back to ``legacy_reason`` so callers can treat
        the two fields uniformly during the LENIENT v0.44.x cycle."""
        q = EndpointQuarantine(quarantine_s=60.0)
        q.add(endpoint_guid="guid-legacy", reason="apo_degraded")
        with patch(
            "sovyx.voice.health._quarantine.get_default_quarantine",
            return_value=q,
        ):
            legacy, derived = _snapshot_quarantine_reasons("guid-legacy")
        assert legacy == "apo_degraded"
        # derived falls back to legacy when entry has empty derived_reason
        assert derived == "apo_degraded"

    def test_derived_reason_distinct_from_legacy(self) -> None:
        """Mission C1 LENIENT — derived carries the verdict class."""
        q = EndpointQuarantine(quarantine_s=60.0)
        q.add(
            endpoint_guid="guid-c1",
            reason="apo_degraded",
            derived_reason="vad_frontend_dead",
        )
        with patch(
            "sovyx.voice.health._quarantine.get_default_quarantine",
            return_value=q,
        ):
            legacy, derived = _snapshot_quarantine_reasons("guid-c1")
        assert legacy == "apo_degraded"
        assert derived == "vad_frontend_dead"

    def test_lookup_exception_returns_empty_strings(self) -> None:
        with patch(
            "sovyx.voice.health._quarantine.get_default_quarantine",
            side_effect=RuntimeError("transient quarantine outage"),
        ):
            legacy, derived = _snapshot_quarantine_reasons("guid-x")
        assert (legacy, derived) == ("", "")


class TestKernelInvalidatedRecheckEligibility:
    """T2.1.a §20.H — kernel rechecker filters by recheck eligibility."""

    @pytest.mark.asyncio()
    async def test_round_skips_ineligible_reasons(self) -> None:
        """Entries with ``derived_reason="vad_frontend_dead"`` /
        ``"format_mismatch"`` are NOT re-probed by the kernel
        rechecker — their recovery path is the in-pipeline reset
        ladder BEFORE quarantine.

        Test fixture uses production-realistic field values: the
        LENIENT v0.44.x coordinator pins ``reason="apo_degraded"`` on
        every quarantine and writes the verdict class to
        ``derived_reason``. The rechecker MUST consult
        ``derived_reason or reason`` to see the verdict class — a bare
        ``entry.reason`` read would always return ``"apo_degraded"``
        and admit every entry past the filter regardless of the
        underlying verdict.
        """
        from sovyx.voice.health._kernel_invalidated_recheck import (
            KernelInvalidatedRechecker,
        )

        q = EndpointQuarantine(quarantine_s=60.0)
        # LENIENT v0.44.x semantics — ``reason`` is the legacy lifecycle
        # pin; ``derived_reason`` carries the verdict class.
        q.add(
            endpoint_guid="guid-apo",
            reason="apo_degraded",
            derived_reason="apo_degraded",
        )
        q.add(
            endpoint_guid="guid-vad",
            reason="apo_degraded",  # legacy pin
            derived_reason="vad_frontend_dead",  # verdict class
        )
        q.add(
            endpoint_guid="guid-fmt",
            reason="apo_degraded",  # legacy pin
            derived_reason="format_mismatch",  # verdict class
        )

        probe = AsyncMock()
        rechecker = KernelInvalidatedRechecker(
            quarantine=q,
            probe_entry=probe,
            interval_s=60.0,
        )
        # Bypass the lifecycle gate — ``_round`` short-circuits its
        # inner loop on ``self._started=False`` so we'd see the round
        # filter log fire but the probe never get called. Setting the
        # flag mimics what ``start()`` does without spawning a task.
        rechecker._started = True  # noqa: SLF001 — exercise the filter

        await rechecker._round()  # noqa: SLF001 — exercising filter

        # Only the APO-degraded entry should reach the probe; the two
        # verdicts filtered out by ``is_recheck_eligible`` evaluated
        # on the derived_reason (NOT the legacy reason pin).
        called_guids = [call.args[0].endpoint_guid for call in probe.call_args_list]
        assert "guid-apo" in called_guids
        assert "guid-vad" not in called_guids
        assert "guid-fmt" not in called_guids


class TestApoClassReasonHelper:
    """T2.1.a §20.H — verify the helper used by the watchdog filter.

    Note on classifier scope: the v0.44.0 ``_APO_CLASS_REASONS`` set
    spans ``{apo_degraded, vad_frontend_dead, format_mismatch}``
    because the watchdog's APO-recheck loop runs a COLD probe whose
    recovery semantics are mechanically similar across those three
    classes. The :func:`is_recheck_eligible` classifier excludes the
    two new ones in the *kernel-invalidated* rechecker because that
    rechecker re-probes via the orchestrator and cannot recover
    Sovyx-side state (ladder fires only BEFORE quarantine, never
    after). The two filters intentionally diverge.
    """

    def test_apo_degraded_is_apo_class(self) -> None:
        from sovyx.voice.health._quarantine import is_apo_class_reason

        assert is_apo_class_reason("apo_degraded") is True

    def test_c1_reasons_included_in_apo_class_for_watchdog_recheck(
        self,
    ) -> None:
        """``vad_frontend_dead`` / ``format_mismatch`` ARE in the
        APO-class set (see classifier docstring) — the watchdog's
        APO-recheck loop reattempts them under the same heuristic."""
        from sovyx.voice.health._quarantine import is_apo_class_reason

        assert is_apo_class_reason("vad_frontend_dead") is True
        assert is_apo_class_reason("format_mismatch") is True

    def test_driver_silent_is_not_apo_class(self) -> None:
        """``driver_silent`` recovery is cascade re-walk, not APO
        bypass — distinct remediation, distinct loop."""
        from sovyx.voice.health._quarantine import is_apo_class_reason

        assert is_apo_class_reason("driver_silent") is False

    def test_unknown_reason_is_not_apo_class(self) -> None:
        from sovyx.voice.health._quarantine import is_apo_class_reason

        assert is_apo_class_reason("") is False
        assert is_apo_class_reason("watchdog_recheck") is False


class TestRecheckEligibleHelper:
    """T1.7.b consumer assertions — kernel rechecker eligibility."""

    def test_apo_degraded_is_recheck_eligible(self) -> None:
        from sovyx.voice.health._quarantine import is_recheck_eligible

        assert is_recheck_eligible("apo_degraded") is True

    def test_new_c1_reasons_are_not_recheck_eligible(self) -> None:
        from sovyx.voice.health._quarantine import is_recheck_eligible

        assert is_recheck_eligible("vad_frontend_dead") is False
        assert is_recheck_eligible("format_mismatch") is False

    def test_watchdog_recheck_reason_is_eligible(self) -> None:
        """Re-added entries from TTL extension keep their lifecycle tag
        ``"watchdog_recheck"`` on ``reason``; the eligibility check
        sees that AND inherits ``derived_reason`` from the prior entry
        (per T1.7.a). The eligibility helper is queried on the LEGACY
        ``reason`` field only; the actual verdict class lives on
        ``derived_reason``. This contract is intentional: a re-added
        TTL-extended entry remains eligible for the next recheck
        cycle (the watchdog re-add is itself a recheck-loop output)."""
        from sovyx.voice.health._quarantine import is_recheck_eligible

        assert is_recheck_eligible("watchdog_recheck") is True
