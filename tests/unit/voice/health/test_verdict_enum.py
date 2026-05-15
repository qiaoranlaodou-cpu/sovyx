"""Closed-set + StrEnum regression guards for IntegrityVerdict / BypassVerdict.

Mission anchor: ``docs-internal/missions/MISSION-c1-vad-mute-reclassification-2026-05-14.md``
§T1.1 + §9.1 + §20.O.

Mirrors :class:`tests.unit.voice.health.test_contract.TestDiagnosisEnum`
for the :class:`Diagnosis` enum. The closed-set assertion is the canonical
regression guard against silent drops of an enum member (anti-pattern #9:
StrEnum value strings are part of the public dashboard / telemetry / log
contract — adding is staged-adoption-safe, dropping is breaking).
"""

from __future__ import annotations

from sovyx.voice.health.contract import BypassVerdict, IntegrityVerdict


class TestIntegrityVerdict:
    """Mission C1 T1.1 — IntegrityVerdict must be a stable StrEnum."""

    def test_is_strenum(self) -> None:
        # anti-pattern #9 — StrEnum guarantees value-based comparison and
        # immunity to xdist namespace duplication.
        assert issubclass(IntegrityVerdict, str)
        assert IntegrityVerdict.HEALTHY == "healthy"

    def test_string_equality_xdist_safe(self) -> None:
        # xdist-safe: value comparison must work even if the class is
        # reimported by a worker (anti-pattern #8).
        assert IntegrityVerdict.HEALTHY == "healthy"
        assert IntegrityVerdict.APO_DEGRADED == "apo_degraded"
        assert IntegrityVerdict.VAD_MUTE.value == "vad_mute"

    def test_value_set_present(self) -> None:
        # Closed-set regression guard — see test_contract.py::TestDiagnosisEnum
        # for the canonical pattern. Dropping a member here is a breaking
        # change to dashboards and telemetry label sets; adding requires
        # extending this set in the same commit.
        expected = {
            "healthy",
            "apo_degraded",
            "driver_silent",
            "vad_mute",
            # Mission C1 T1.1 — VAD-frontend-dead is the new first-class
            # classification (was incorrectly folded into VAD_MUTE pre-v0.44.0,
            # see forensic anchor docs-internal/FORENSIC-AUDIT-LOG-2026-05-14-v0.43.1.md
            # §C1). Routes to a Silero/normalizer/AGC2 reset ladder.
            "vad_frontend_dead",
            # Mission C1 T1.1 — Format-mismatch is the second new verdict.
            # Routes to AudioCaptureTask.engage_frame_normalizer() force-reopen
            # path, NOT to the OS-layer bypass strategy ladder.
            "format_mismatch",
            "inconclusive",
        }
        assert {v.value for v in IntegrityVerdict} == expected

    def test_new_members_constructible_by_value(self) -> None:
        # Mission §9.1 — positive round-trip for the new members. Confirms
        # both StrEnum value-constructor lookup and identity stability.
        assert IntegrityVerdict("vad_frontend_dead") is IntegrityVerdict.VAD_FRONTEND_DEAD
        assert IntegrityVerdict("format_mismatch") is IntegrityVerdict.FORMAT_MISMATCH

    def test_new_members_distinct_from_vad_mute(self) -> None:
        # Regression guard against the misclassification that motivated
        # mission C1: VAD_MUTE (benign — user not speaking) and
        # VAD_FRONTEND_DEAD (Silero/normalizer fault) MUST be distinct
        # values, dispatched to disjoint remediation ladders.
        # Anti-pattern #39(a).
        assert IntegrityVerdict.VAD_MUTE is not IntegrityVerdict.VAD_FRONTEND_DEAD
        assert IntegrityVerdict.VAD_MUTE.value != IntegrityVerdict.VAD_FRONTEND_DEAD.value
        assert IntegrityVerdict.VAD_MUTE is not IntegrityVerdict.FORMAT_MISMATCH
        assert IntegrityVerdict.VAD_MUTE.value != IntegrityVerdict.FORMAT_MISMATCH.value

    def test_member_name_to_value_invariant(self) -> None:
        # All members follow snake_case-of-name convention (lowercased
        # underscore form of the canonical name). Future additions must
        # honor this so dashboards can reverse the mapping reliably.
        for member in IntegrityVerdict:
            assert member.value == member.name.lower()


class TestBypassVerdict:
    """Mission C1 — BypassVerdict regression guard post-T1.5.

    Pins the v0.44.0 BypassVerdict value set. The 4 new variants
    (VAD_FRONTEND_RESET_APPLIED_HEALTHY / VAD_FRONTEND_RESET_APPLIED_STILL_DEAD
    / CASCADE_REEVALUATION_REQUESTED / NORMALIZER_ENGAGEMENT_REQUESTED) are
    routed through telemetry helpers OTHER than record_bypass_strategy_verdict
    — see _C1_NON_STRATEGY_VERDICTS in _bypass_tier_state.py + the
    BypassVerdict docstring for the routing contract.
    """

    def test_is_strenum(self) -> None:
        assert issubclass(BypassVerdict, str)
        assert BypassVerdict.APPLIED_HEALTHY == "applied_healthy"

    def test_value_set_present(self) -> None:
        # Mission C1 T1.5 — closed-set guard for the v0.44.0 BypassVerdict
        # value set. Any drop / rename is a breaking contract change for
        # downstream telemetry dashboards and the _bypass_tier_state
        # filter constants. Adding a new value requires extending this
        # set in the same commit + classifying the new value as
        # strategy-tier eligible OR non-strategy per the BypassVerdict
        # docstring routing contract.
        expected = {
            # Legacy 5 — strategy attempt outcomes routed through
            # record_bypass_strategy_verdict + _bypass_tier_state.
            "applied_healthy",
            "applied_still_dead",
            "not_applicable",
            "failed_to_apply",
            "reverted",
            # Mission C1 T1.5 — VAD-frontend reset ladder step outcomes.
            # Routed through record_vad_frontend_reset_outcome.
            "vad_frontend_reset_applied_healthy",
            "vad_frontend_reset_applied_still_dead",
            # Mission C1 T1.5 — coordinator dispatch decisions.
            # Routed through record_coordinator_outcome.
            "cascade_reevaluation_requested",
            "normalizer_engagement_requested",
        }
        assert {v.value for v in BypassVerdict} == expected

    def test_new_members_constructible_by_value(self) -> None:
        # Round-trip — confirms StrEnum value-constructor lookup and
        # identity stability for the new T1.5 members.
        assert (
            BypassVerdict("vad_frontend_reset_applied_healthy")
            is BypassVerdict.VAD_FRONTEND_RESET_APPLIED_HEALTHY
        )
        assert (
            BypassVerdict("vad_frontend_reset_applied_still_dead")
            is BypassVerdict.VAD_FRONTEND_RESET_APPLIED_STILL_DEAD
        )
        assert (
            BypassVerdict("cascade_reevaluation_requested")
            is BypassVerdict.CASCADE_REEVALUATION_REQUESTED
        )
        assert (
            BypassVerdict("normalizer_engagement_requested")
            is BypassVerdict.NORMALIZER_ENGAGEMENT_REQUESTED
        )
