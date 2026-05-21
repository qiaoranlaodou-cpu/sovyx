"""F-008/F-009/F-010/F-011/F-013 regression — MISSION-A.2.P5 operator-hint disclosures.

Mission anchor:
``docs-internal/missions/MISSION-A2-operator-trust-remediation-2026-05-20.md``
§T1.4..T1.5 Phase A.2.P5.

The Mission A audit (§7) catalogued five operator-trust hazards in
the ``_resource_remediation.py`` registry that did not disclose
known semantic edge cases — operators reading the hints could draw
incorrect conclusions about field values. This file mechanically
anchors the disclosure phrasing so a future commit dropping the
operator-trust language fails the test rather than silently regressing.

Each disclosure is keyed by the audit finding ID:

  - F-008: container ``memory_percent`` reads host total, not cgroup
  - F-009: ``num_handles_or_fds`` cross-platform magnitude differs
  - F-010: ``cpu_percent`` first-tick returns 0.0
  - F-011: ``cpu_times_*_s`` are cumulative (derivative required)
  - F-013: ``connections_count`` Linux non-root sees only own
"""

from __future__ import annotations

from sovyx.observability._resource_remediation import FIELD_REMEDIATIONS


class TestOperatorHintDisclosures:
    """F-008–F-013 — every documented hazard surfaces in the operator hint."""

    def test_f008_memory_percent_discloses_cgroup_vs_host(self) -> None:
        """F-008: containerized operators MUST learn host vs cgroup."""
        hint = FIELD_REMEDIATIONS["process.memory_percent"]
        # Mandatory keywords for the F-008 disclosure.
        for keyword in ("cgroup", "host", "container"):
            assert keyword.lower() in hint.lower(), (
                f"F-008 disclosure missing keyword {keyword!r} in "
                f"process.memory_percent hint; container operators would not "
                "learn that psutil reads host total rather than cgroup limit."
            )

    def test_f009_num_handles_or_fds_discloses_per_platform_baselines(self) -> None:
        """F-009: Windows vs POSIX magnitude difference MUST be disclosed."""
        hint = FIELD_REMEDIATIONS["process.num_handles_or_fds"]
        # Both platform names + magnitude hints must appear.
        for keyword in ("Windows", "POSIX", "30", "2,000"):
            assert keyword in hint, (
                f"F-009 disclosure missing keyword {keyword!r}; cross-platform "
                "operators would not see that Windows num_handles() spans "
                "2,000-10,000+ vs POSIX num_fds() at 30-80."
            )

    def test_f010_cpu_percent_discloses_first_tick_calibration(self) -> None:
        """F-010: first snapshot's 0.0 reading MUST be disclosed."""
        hint = FIELD_REMEDIATIONS["process.cpu_percent"]
        for keyword in ("0.0", "first", "calibration"):
            assert keyword.lower() in hint.lower(), (
                f"F-010 disclosure missing keyword {keyword!r}; operators alerting "
                "on CPU would treat the first-tick 0.0 as 'idle' rather than "
                "'no prior sample to delta against'."
            )

    def test_f011_cpu_times_discloses_derivative_requirement(self) -> None:
        """F-011: ``cpu_times_*_s`` cumulative-not-instantaneous MUST be disclosed."""
        for field in ("process.cpu_times_user_s", "process.cpu_times_system_s"):
            hint = FIELD_REMEDIATIONS[field]
            for keyword in ("cumulative", "derivative", "monotonic"):
                assert keyword.lower() in hint.lower(), (
                    f"F-011 disclosure missing keyword {keyword!r} in {field} hint; "
                    "operators reading the raw value as a rate would draw "
                    "incorrect conclusions."
                )
        # The user_s hint MUST include a concrete derivative formula example
        # (operator-actionability requirement per the F-011 finding).
        assert "snapshot_taken_at_monotonic" in FIELD_REMEDIATIONS["process.cpu_times_user_s"], (
            "F-011 disclosure SHOULD include a concrete Δ/Δt formula example."
        )

    def test_f013_connections_count_discloses_cap_net_admin(self) -> None:
        """F-013: Linux CAP_NET_ADMIN requirement MUST be disclosed."""
        # F-013 is split: the count hint cross-references the status hint
        # which carries the CAP_NET_ADMIN disclosure.
        status_hint = FIELD_REMEDIATIONS["process.connections_status"]
        assert "CAP_NET_ADMIN" in status_hint, (
            "F-013 disclosure: process.connections_status hint MUST cite "
            "CAP_NET_ADMIN — Linux non-root operators see only the daemon's "
            "own connections without this capability."
        )

    def test_no_stale_pending_count_references(self) -> None:
        """Post-MISSION-A.1.P3.b: hints reference ``awaiting_count``, not ``pending_count``.

        ``asyncio.pending_count`` is a LENIENT shim (sunset v0.55.0). Operator
        hints (the operator-facing surface) MUST point at the post-A.1 canonical
        ``awaiting_count`` so operators learn the new name.

        Two exceptions:
          - the ``awaiting_count`` hint itself documents the rename (its
            cross-reference to ``pending_count`` is intentional).
          - hints that explain the rename also pair both names.
        """
        # The awaiting_count hint is the canonical place to document the
        # legacy ``pending_count`` shim — exempt from the orphan check.
        exempt_fields = {"asyncio.awaiting_count", "asyncio.not_done_count"}
        for field, hint in FIELD_REMEDIATIONS.items():
            if field in exempt_fields:
                continue
            if "pending_count" in hint:
                assert "awaiting_count" in hint, (
                    f"Hint for {field!r} mentions ``pending_count`` (legacy "
                    "shim, sunset v0.55.0) without pairing with the post-"
                    "MISSION-A.1.P3.b canonical name ``awaiting_count``. "
                    "Operators reading this hint would learn the deprecated name."
                )
