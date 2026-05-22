"""Mission C Phase C.10 — LENIENT sunset assertion harness.

Mechanical forcing function for the v0.51.0 (H2) + v0.55.0 (A.1 cluster)
STRICT-flip transitions. Each LENIENT shim shipped by ADR-D14 / ADR-D15
/ ADR-D16 / Mission H2 carries a paired test in this file that:

1. **Pre-sunset (current __version__ < sunset target):** asserts the
   dual-emit / fallback / map entry IS present. Proof that the LENIENT
   shim is actually wired — drift to "absent" pre-sunset would silently
   break operator dashboards / log forwarders keyed on the legacy
   identifier.
2. **At/post-sunset (current __version__ >= sunset target):** asserts
   the legacy form IS ABSENT. The forcing function: any developer
   bumping ``pyproject.toml`` past the sunset target version WITHOUT
   removing the corresponding shim will get a hard pytest failure,
   blocking the tag-cut at ``scripts/verify_gates.sh`` Gate 5.

Findings closed:

* **C-P1-13** — ``test_engine_resources_boundary.py`` regains the F-FAL-5
  contract assertion (handled in that file; this harness adds the
  cross-version mechanical fence).
* **C-P1-16** — H2 dual-emit wrapper v0.51.0 STRICT-flip falsifiability.
* **C-P1-17** — Governor LENIENT fallback v0.55.0 sunset.

Sunset target inventory (each one a paired pre/post assertion):

| # | Surface | Legacy form | Canonical form | ADR | Sunset tag |
|---|---|---|---|---|---|
| 1 | snapshotter | ``exception_cohort.retained_bytes_estimate`` | ``exception_cohort.cumulative_retained_bytes_since_start`` | ADR-D14 | v0.55.0 |
| 2 | snapshotter | ``exception_cohort.distinct_group_id_count`` | ``exception_cohort.cumulative_distinct_group_id_count`` | ADR-D14 | v0.55.0 |
| 3 | snapshotter | ``to_thread.active_workers`` | ``to_thread.pool_size_at_last_dispatch`` | ADR-D15 (+D16 shim-of-shim) | v0.55.0 |
| 4 | snapshotter | ``asyncio.current_running_task_name`` | ``asyncio.all_task_names`` | ADR-D15 | v0.55.0 |
| 5 | snapshotter | ``to_thread.pool_size`` | ``to_thread.pool_size_at_last_dispatch`` | ADR-D16 | v0.55.0 |
| 6 | snapshotter | ``to_thread.max_workers`` | ``to_thread.max_workers_at_last_dispatch`` | ADR-D16 | v0.55.0 |
| 7 | snapshotter | ``to_thread.queue_depth`` | ``to_thread.queue_depth_at_last_dispatch`` | ADR-D16 | v0.55.0 |
| 8 | snapshotter | ``asyncio.running_count`` | ``asyncio.not_done_count`` | ADR-D16 | v0.55.0 |
| 9 | snapshotter | ``asyncio.pending_count`` | ``asyncio.awaiting_count`` | ADR-D16 | v0.55.0 |
| 10 | governor | ``exception_cohort.retained_bytes_estimate`` fallback read | ``exception_cohort.window_retained_bytes`` (canonical read) | (consumer-side of ADR-D14) | v0.55.0 |
| 11 | H2 wrapper | ``LEGACY_TWIN_MAP`` non-empty | (map emptied or removed) | H2 ADR + anti-pattern #45 | v0.51.0 |

Out of scope (separate sunset cycles):

* ``system.rss_bytes`` (ADR-D9) — H4 v0.54.0 STRICT cycle; not
  Mission C.10's sunset target.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.10
+ ``docs-internal/MISSION-C-FINDINGS-REGISTER-2026-05-21.md`` C-P1-13/16/17.

ADR anchors:
* ``docs-internal/ADR-D14-exception-cohort-window-vs-lifetime.md``
* ``docs-internal/ADR-D15-asyncio-and-to-thread-semantic-lie-retirement.md``
* ``docs-internal/ADR-D16-twin-name-freshness-disambiguation.md``
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Final
from unittest.mock import MagicMock, patch

import pytest

import sovyx.observability.resources as resources_mod
from sovyx import __version__
from sovyx.observability._resource_registry import (
    record_to_thread_dispatch,
    reset_default_resource_registry,
)
from sovyx.observability.resources import ResourceSnapshotter
from sovyx.voice._event_names import (
    CAPTURE_INTEGRITY_EVENT_NAMES,
    CaptureIntegrityEvent,
)

# ── Version comparison (dep-free) ────────────────────────────────────────


def _version_tuple(value: str) -> tuple[int, ...]:
    """Parse a PEP-440-ish ``X.Y.Z`` string into a comparable int tuple.

    Tolerates pre-release / dev suffixes by truncating at the first
    non-numeric component. Sovyx never ships rc/dev cadence per
    ``feedback_no_rc_cadence``; the truncation is defensive only.
    """
    parts: list[int] = []
    for segment in value.split("."):
        digit_prefix = ""
        for ch in segment:
            if ch.isdigit():
                digit_prefix += ch
            else:
                break
        if not digit_prefix:
            break
        parts.append(int(digit_prefix))
    return tuple(parts)


def _sunset_reached(sunset_version: str) -> bool:
    """Return True iff the current ``__version__`` is at/past sunset.

    At-sunset: the legacy shim MUST be removed. Pre-sunset: the legacy
    shim MUST be present (operator-dashboard compatibility window).
    """
    return _version_tuple(__version__) >= _version_tuple(sunset_version)


_SUNSET_A1_CLUSTER: Final[str] = "0.55.0"
"""ADR-D14 + ADR-D15 + ADR-D16 + governor LENIENT fallback sunset tag.

A single operator validation gate (V-A1-FINAL) covers all 10 surfaces
at the v0.55.0 cut. See ADR-D16 §"v0.55.0 STRICT-flip" for the full
inventory.
"""

_SUNSET_H2: Final[str] = "0.51.0"
"""Mission H2 dual-emit wrapper sunset tag (Gate 13 STRICT flip).

The H2 wrapper (:func:`sovyx.voice.pipeline._capture_integrity_emit
.emit_capture_integrity_event`) dual-emits the neutral event name +
the legacy twin from :data:`LEGACY_TWIN_MAP` during the v0.49.6..v0.50.x
LENIENT window. At v0.51.0 the legacy emission block (and ideally the
map itself) is removed.
"""


# ── Snapshot capture (matches test_resources_h4_extension.py pattern) ────


@pytest.fixture(autouse=True)
def _reset_singleton() -> Generator[None, None, None]:
    reset_default_resource_registry()
    yield
    reset_default_resource_registry()


@pytest.fixture()
def snapshotter() -> ResourceSnapshotter:
    config = MagicMock()
    config.sampling.perf_hotpath_interval_seconds = 60
    return ResourceSnapshotter(config)


def _capture_emit(snap: ResourceSnapshotter) -> dict[str, object]:
    """Trigger ``_emit_snapshot`` and return the ``self.health.snapshot``
    kwargs payload.

    Mirrors the H4 extension test capture pattern so the harness uses
    the same evidence surface (the actual emitted dict) rather than a
    parallel synthetic source.
    """
    with patch.object(resources_mod.logger, "info") as info, patch("psutil.Process"):
        snap._emit_snapshot(final=False)
    snapshot_calls = [
        c for c in info.call_args_list if c.args and c.args[0] == "self.health.snapshot"
    ]
    assert snapshot_calls, "logger.info('self.health.snapshot', ...) MUST be called"
    return dict(snapshot_calls[-1].kwargs)


# ── Pre/post sunset fence helpers ────────────────────────────────────────


def _assert_lenient_or_sunset(
    payload: dict[str, object],
    *,
    legacy_key: str,
    canonical_key: str,
    sunset_version: str,
) -> None:
    """Mechanical sunset fence on a snapshot payload.

    Pre-sunset (``__version__ < sunset_version``):

    * ``legacy_key`` MUST be in ``payload`` — the LENIENT dual-emit is
      load-bearing for external operator dashboards.
    * ``canonical_key`` MUST also be in ``payload`` — the dual-emit
      requires both names live during the window.
    * The two values MUST be equal — the legacy shim is a literal alias
      sourced from the canonical (per ADR-D14/D15/D16 snapshotter
      emission contract). Mismatch indicates a wrong-source emission
      (F-FAL-5 hazard catalogued in C-P1-13).

    At/post-sunset (``__version__ >= sunset_version``):

    * ``legacy_key`` MUST NOT be in ``payload`` — the LENIENT shim is
      removed; only the canonical key emits.
    """
    assert canonical_key in payload, (
        f"canonical key {canonical_key!r} MUST always emit "
        f"(LENIENT or STRICT); got payload keys {sorted(payload)}"
    )
    if _sunset_reached(sunset_version):
        assert legacy_key not in payload, (
            f"LENIENT shim {legacy_key!r} MUST be removed at sunset "
            f"v{sunset_version} (current version v{__version__}); "
            f"see ADR-D14/D15/D16 §v0.55.0 STRICT-flip."
        )
    else:
        assert legacy_key in payload, (
            f"LENIENT shim {legacy_key!r} MUST dual-emit pre-sunset "
            f"v{sunset_version} (current version v{__version__}); "
            f"removal pre-sunset breaks operator dashboards keyed on "
            f"the legacy identifier."
        )
        # Both keys present — pin the literal-alias contract (the
        # F-FAL-5 wrong-source hazard).
        assert payload[legacy_key] == payload[canonical_key], (
            f"LENIENT shim {legacy_key!r} value drifted from canonical "
            f"{canonical_key!r}: legacy={payload[legacy_key]!r} vs "
            f"canonical={payload[canonical_key]!r}. The snapshotter "
            f"emits the shim as a literal alias of the canonical; "
            f"mismatch indicates a wrong-source emission "
            f"(anti-pattern #51 / F-FAL-5)."
        )


# ── A.1 cluster sunset assertions (v0.55.0) ──────────────────────────────


class TestADRD14ExceptionCohortSunset:
    """ADR-D14 — exception_cohort cumulative-vs-window dual-emit.

    Pre-v0.55.0: legacy ``retained_bytes_estimate`` /
    ``distinct_group_id_count`` shadow the new ``cumulative_*`` keys.
    Post-v0.55.0: only the cumulative canonicals emit.
    """

    def test_retained_bytes_estimate_sunset_at_v055(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="exception_cohort.retained_bytes_estimate",
            canonical_key="exception_cohort.cumulative_retained_bytes_since_start",
            sunset_version=_SUNSET_A1_CLUSTER,
        )

    def test_distinct_group_id_count_sunset_at_v055(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="exception_cohort.distinct_group_id_count",
            canonical_key="exception_cohort.cumulative_distinct_group_id_count",
            sunset_version=_SUNSET_A1_CLUSTER,
        )


class TestADRD15SemanticLieRetirement:
    """ADR-D15 — ``to_thread.active_workers`` + ``asyncio.current_running_task_name``.

    Both were semantic lies (F-006 / F-005); they remain as LENIENT
    shims during the v0.49.x..v0.54.x window so external consumers
    keep parsing snapshots.
    """

    def test_to_thread_active_workers_sunset_at_v055(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        # Touch the to_thread registry so pool_size_at_last_dispatch is
        # populated; the snapshotter only emits the legacy alias when
        # the canonical is an int (defensive guard at resources.py:535).
        record_to_thread_dispatch(
            label="c10-touch", worker_count_at_dispatch=1, queue_depth=0, max_workers=4
        )
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="to_thread.active_workers",
            canonical_key="to_thread.pool_size_at_last_dispatch",
            sunset_version=_SUNSET_A1_CLUSTER,
        )

    def test_current_running_task_name_sunset_at_v055(
        self, snapshotter: ResourceSnapshotter
    ) -> None:
        """``asyncio.current_running_task_name`` always emits during the
        async snapshot — observation-paradox shim (F-005). Sunset drops
        it; consumers read ``asyncio.all_task_names`` instead.
        """
        payload = _capture_emit(snapshotter)
        if _sunset_reached(_SUNSET_A1_CLUSTER):
            assert "asyncio.current_running_task_name" not in payload, (
                f"LENIENT shim 'asyncio.current_running_task_name' MUST "
                f"be removed at sunset v{_SUNSET_A1_CLUSTER}; current "
                f"version v{__version__}."
            )
        else:
            assert "asyncio.current_running_task_name" in payload, (
                f"LENIENT shim 'asyncio.current_running_task_name' MUST "
                f"dual-emit pre-sunset v{_SUNSET_A1_CLUSTER}; current "
                f"version v{__version__}."
            )
        # Canonical replacement MUST always emit (LENIENT or STRICT).
        assert "asyncio.all_task_names" in payload, (
            "ADR-D15 canonical 'asyncio.all_task_names' MUST always emit; "
            f"got payload keys {sorted(payload)}"
        )


class TestADRD16TwinNameFreshness:
    """ADR-D16 — ``to_thread.{pool_size,max_workers,queue_depth}`` +
    ``asyncio.{running_count,pending_count}`` LENIENT shims.

    F-007 / F-014 closures. Each legacy key carries the same value as
    its ``_at_last_dispatch`` / canonical replacement during the
    LENIENT window.
    """

    def test_pool_size_sunset_at_v055(self, snapshotter: ResourceSnapshotter) -> None:
        record_to_thread_dispatch(
            label="c10-touch", worker_count_at_dispatch=2, queue_depth=1, max_workers=4
        )
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="to_thread.pool_size",
            canonical_key="to_thread.pool_size_at_last_dispatch",
            sunset_version=_SUNSET_A1_CLUSTER,
        )

    def test_max_workers_sunset_at_v055(self, snapshotter: ResourceSnapshotter) -> None:
        record_to_thread_dispatch(
            label="c10-touch", worker_count_at_dispatch=2, queue_depth=1, max_workers=4
        )
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="to_thread.max_workers",
            canonical_key="to_thread.max_workers_at_last_dispatch",
            sunset_version=_SUNSET_A1_CLUSTER,
        )

    def test_queue_depth_sunset_at_v055(self, snapshotter: ResourceSnapshotter) -> None:
        record_to_thread_dispatch(
            label="c10-touch", worker_count_at_dispatch=2, queue_depth=1, max_workers=4
        )
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="to_thread.queue_depth",
            canonical_key="to_thread.queue_depth_at_last_dispatch",
            sunset_version=_SUNSET_A1_CLUSTER,
        )

    def test_running_count_sunset_at_v055(self, snapshotter: ResourceSnapshotter) -> None:
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="asyncio.running_count",
            canonical_key="asyncio.not_done_count",
            sunset_version=_SUNSET_A1_CLUSTER,
        )

    def test_pending_count_sunset_at_v055(self, snapshotter: ResourceSnapshotter) -> None:
        payload = _capture_emit(snapshotter)
        _assert_lenient_or_sunset(
            payload,
            legacy_key="asyncio.pending_count",
            canonical_key="asyncio.awaiting_count",
            sunset_version=_SUNSET_A1_CLUSTER,
        )


class TestGovernorLenientFallbackSunset:
    """C-P1-17 — Governor LENIENT fallback reads legacy
    ``exception_cohort.retained_bytes_estimate`` when the canonical
    ``exception_cohort.window_retained_bytes`` is missing.

    At v0.55.0 the fallback branch is removed; the governor reads only
    the canonical and returns INSUFFICIENT_DATA if absent. The mechanical
    proof: AST-scan the governor source for the legacy key.
    """

    def test_governor_fallback_read_pre_sunset(self) -> None:
        """Pre-v0.55.0: the legacy key string literal MUST appear in
        the governor source (the fallback branch is live).

        Post-v0.55.0: the literal MUST be gone (the fallback branch is
        deleted as part of the STRICT flip).
        """
        from pathlib import Path

        governor_src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "sovyx"
            / "observability"
            / "_resource_cohort_governor.py"
        )
        source = governor_src.read_text(encoding="utf-8")
        legacy_literal = '"exception_cohort.retained_bytes_estimate"'
        if _sunset_reached(_SUNSET_A1_CLUSTER):
            assert legacy_literal not in source, (
                f"governor LENIENT fallback {legacy_literal!r} MUST be "
                f"removed at sunset v{_SUNSET_A1_CLUSTER}; current "
                f"version v{__version__}. See "
                f"_resource_cohort_governor.py:591-595."
            )
        else:
            assert legacy_literal in source, (
                f"governor LENIENT fallback {legacy_literal!r} MUST be "
                f"live pre-sunset v{_SUNSET_A1_CLUSTER}; removal "
                f"pre-sunset breaks the governor on pre-A.1 snapshots."
            )


# ── H2 dual-emit wrapper sunset (v0.51.0) ────────────────────────────────


class TestH2DualEmitWrapperSunset:
    """C-P1-16 — H2 dual-emit wrapper has env-knob kill switch but no
    test asserts the v0.51.0 STRICT-flip.

    The wrapper at ``voice/pipeline/_capture_integrity_emit.py:253``
    looks up the legacy name via ``LEGACY_TWIN_MAP[event]``. Sunset
    means the legacy emission block (and the map itself) is removed.
    """

    def test_legacy_twin_map_sunset_at_v051(self) -> None:
        """Pre-v0.51.0: ``LEGACY_TWIN_MAP`` MUST contain one entry per
        :class:`CaptureIntegrityEvent` member.

        At/post-v0.51.0: ``LEGACY_TWIN_MAP`` MUST be empty (or removed
        from the module). The dual-emit block is gone; only the neutral
        names emit.
        """
        from sovyx.voice import _event_names

        map_obj = getattr(_event_names, "LEGACY_TWIN_MAP", None)
        if _sunset_reached(_SUNSET_H2):
            assert not map_obj, (
                f"H2 LEGACY_TWIN_MAP MUST be empty or removed at sunset "
                f"v{_SUNSET_H2}; current version v{__version__}. "
                f"Got {map_obj!r}."
            )
        else:
            assert map_obj is not None, (
                f"H2 LEGACY_TWIN_MAP MUST be defined pre-sunset "
                f"v{_SUNSET_H2}; current version v{__version__}."
            )
            # Map MUST cover every neutral event member during LENIENT.
            covered = {e for e in CaptureIntegrityEvent if e in map_obj}
            missing = set(CaptureIntegrityEvent) - covered
            assert not missing, (
                f"H2 LEGACY_TWIN_MAP missing legacy twin for members "
                f"{sorted(e.value for e in missing)}; every neutral "
                f"event MUST dual-emit pre-sunset v{_SUNSET_H2}."
            )
            # Sanity: the legacy values MUST be distinct (no two
            # neutrals collapsing onto the same legacy literal).
            legacy_values = list(map_obj.values())
            assert len(legacy_values) == len(set(legacy_values)), (
                f"H2 LEGACY_TWIN_MAP has duplicate legacy values: {sorted(legacy_values)}"
            )

    def test_dual_emit_kill_switch_env_var_pinned(self) -> None:
        """The kill-switch env var name is part of the operator-facing
        contract documented in the v0.51.0 STRICT-flip release notes.
        Drift of the env var name pre-sunset silently disables the
        operator's ability to opt out (C-P1-16 hazard).
        """
        from sovyx.voice.pipeline import _capture_integrity_emit

        expected = "SOVYX_TUNING__VOICE__CAPTURE_INTEGRITY_DUAL_EMIT_ENABLED"
        assert expected == _capture_integrity_emit._ENV_KNOB, (
            f"H2 dual-emit kill-switch env var name drifted: "
            f"expected {expected!r}, got "
            f"{_capture_integrity_emit._ENV_KNOB!r}. The operator-facing "
            f"name is part of the v0.51.0 STRICT-flip release contract."
        )


# ── Sunset inventory drift fence ─────────────────────────────────────────


class TestSunsetInventoryCoverage:
    """Drift fence: every ``# a1-allowlist: legacy alias, sunset v0.55.0``
    occurrence in the snapshotter MUST have a paired test in this file.

    Forcing function: adding a new LENIENT shim to the snapshotter
    without a paired sunset assertion = AST scan reports a delta, this
    test fails with the new shim name.
    """

    _COVERED_A1_LEGACY_KEYS: Final[frozenset[str]] = frozenset(
        {
            "exception_cohort.retained_bytes_estimate",
            "exception_cohort.distinct_group_id_count",
            "to_thread.active_workers",
            "to_thread.pool_size",
            "to_thread.max_workers",
            "to_thread.queue_depth",
            "asyncio.running_count",
            "asyncio.pending_count",
            "asyncio.current_running_task_name",
        },
    )

    def test_a1_shim_inventory_matches_snapshotter(self) -> None:
        """Cross-check coverage: every LENIENT shim emitted by the
        snapshotter MUST appear in ``_COVERED_A1_LEGACY_KEYS``.

        Mechanically scans the snapshotter source for the
        ``a1-allowlist: legacy ...`` comment markers. Adding a new shim
        without an allowlist comment fails Gate 15
        (resource_hygiene_discipline); adding one WITH the comment but
        no paired test in this file fails THIS test.
        """
        import re
        from pathlib import Path

        snapshotter_src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "sovyx"
            / "observability"
            / "resources.py"
        )
        source = snapshotter_src.read_text(encoding="utf-8")

        # Extract the dotted key emitted on lines tagged with the
        # ``a1-allowlist: legacy ... sunset v0.55.0`` comment. The
        # pattern in resources.py is either:
        #   "asyncio.running_count": 0,  # a1-allowlist: legacy shim, ...
        #   registry_with_legacy["to_thread.pool_size"] = ...  # a1-allow...
        # Both put the dotted key in a double-quoted string on the
        # same line. Capture the first such string on each allowlist
        # line.
        pattern = re.compile(
            r'"((?:asyncio|to_thread|exception_cohort)\.[^"]+)"[^#\n]*'
            r"#\s*a1-allowlist:\s*legacy",
        )
        emitted_legacy_keys: set[str] = set()
        for line in source.splitlines():
            match = pattern.search(line)
            if match:
                emitted_legacy_keys.add(match.group(1))

        # Inline comments above the assignment are also allowed; the
        # snapshotter uses that style for the `registry_with_legacy[...]`
        # assignments. Sweep again for that variant: any line of the
        # form `registry_with_legacy["<key>"] = ...` whose IMMEDIATELY
        # PRECEDING non-blank line carries an `a1-allowlist: legacy`
        # comment.
        assign_pattern = re.compile(r'registry_with_legacy\["([^"]+)"\]\s*=')
        lines = source.splitlines()
        for idx, line in enumerate(lines):
            assign_match = assign_pattern.search(line)
            if not assign_match:
                continue
            # Walk backwards skipping blank lines.
            j = idx - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j >= 0 and "a1-allowlist: legacy" in lines[j]:
                key = assign_match.group(1)
                # Filter to A.1 cluster surfaces only — H4 system.rss_bytes
                # has its own `h4-allowlist:` comment marker, not a1.
                if key.startswith(("asyncio.", "to_thread.", "exception_cohort.")):
                    emitted_legacy_keys.add(key)

        missing_from_inventory = emitted_legacy_keys - self._COVERED_A1_LEGACY_KEYS
        assert not missing_from_inventory, (
            f"snapshotter emits LENIENT A.1 shim(s) "
            f"{sorted(missing_from_inventory)} that lack a paired sunset "
            f"assertion in this file. Add a test method covering each "
            f"new key OR extend _COVERED_A1_LEGACY_KEYS if the new shim "
            f"is already covered by a sibling test."
        )
        # Inverse drift: the inventory MUST NOT carry surfaces the
        # snapshotter no longer emits (would mean the shim was removed
        # but the inventory wasn't updated). Tolerated pre-sunset only.
        if not _sunset_reached(_SUNSET_A1_CLUSTER):
            stale_in_inventory = self._COVERED_A1_LEGACY_KEYS - emitted_legacy_keys
            assert not stale_in_inventory, (
                f"_COVERED_A1_LEGACY_KEYS carries surface(s) "
                f"{sorted(stale_in_inventory)} the snapshotter no longer "
                f"emits pre-sunset. Either the shim was removed early "
                f"(restore it OR remove from the inventory) or the "
                f"scanner regex drifted."
            )


# ── Module-import smoke ─────────────────────────────────────────────────


def test_capture_integrity_event_names_constant_exists() -> None:
    """Sanity import — :data:`CAPTURE_INTEGRITY_EVENT_NAMES` MUST exist
    so a future H2 STRICT-flip commit that removes :data:`LEGACY_TWIN_MAP`
    while accidentally removing the neutral set is caught here.
    """
    assert isinstance(CAPTURE_INTEGRITY_EVENT_NAMES, frozenset)
    assert CAPTURE_INTEGRITY_EVENT_NAMES, (
        "neutral event-name SSoT MUST be non-empty across all sunset transitions"
    )
