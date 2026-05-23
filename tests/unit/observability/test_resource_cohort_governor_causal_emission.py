"""Mission OX-1.B — unit tests for the additive ``axis.cleared`` emission
at the governor's HEALTHY-edge clear path.

Contract:

* When ``ResourceCohortGovernor.causal_chain_enabled=True`` AND
  ``_clear_axis_entry_for_reason`` actually evicts an entry
  (``store.clear_reason(...)`` returns True), the function emits
  exactly ONE ``axis.cleared`` INFO log carrying the (axis, reason,
  source) triple.
* When the flag is False (default), NO ``axis.cleared`` event is
  emitted regardless of whether the clear succeeded.
* When ``store.clear_reason(...)`` returns False (nothing to evict —
  the entry was already gone), NO ``axis.cleared`` event is emitted
  even with the flag True.
* The sibling-existing ``engine.resources.cohort_auto_cleared`` event
  remains UNCHANGED in both gated and ungated paths (no shape drift).

Capture strategy: patch the module's ``logger.info`` directly via
``patch.object`` (anti-pattern #36 — preferred for callables already
bound on the target module). Avoids structlog/stdlib bridge plumbing
in unit tests.

xdist-safe per anti-pattern #8.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import sovyx.observability._resource_cohort_governor as _governor_mod
from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    reset_default_degraded_store,
)
from sovyx.observability._resource_cohort_governor import (
    ResourceCohortGovernor,
    _clear_axis_entry_for_reason,
    reset_default_resource_cohort_governor,
)
from sovyx.observability._resource_registry import CohortAxis

_RSS_AXIS = CohortAxis.RSS_GROWTH
_RSS_REASON = "engine_resources.rss_growth_spike"


@pytest.fixture
def _reset_singletons() -> None:
    """Fresh governor + degraded store per test (cross-test isolation
    per anti-pattern #5 + the existing
    ``reset_default_resource_cohort_governor`` test-only helper)."""
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()
    yield
    reset_default_resource_cohort_governor()
    reset_default_degraded_store()


def _install_governor(*, causal_chain_enabled: bool) -> None:
    """Replace the module-level singleton with a governor whose
    ``causal_chain_enabled`` flag matches the test's intent. Bypasses
    ``from_tuning`` to keep the test independent of
    :class:`ObservabilityTuningConfig`."""
    governor = ResourceCohortGovernor(causal_chain_enabled=causal_chain_enabled)
    _governor_mod._SINGLETON = governor  # noqa: SLF001


def _pre_record_rss_entry() -> None:
    """Seed the degraded store with the entry the governor will clear."""
    store = get_default_degraded_store()
    store.record(
        DegradedEntry(
            axis="engine_resources",
            reason=_RSS_REASON,
            severity="warn",
            title_token="degraded.engine_resources.rss_growth.title",
            body_token="degraded.engine_resources.rss_growth.body",
        )
    )


def _info_event_names(mock: MagicMock) -> list[str]:
    """Extract the first positional arg (event name) from each
    ``logger.info(event_name, **fields)`` call recorded by the mock."""
    return [call.args[0] for call in mock.info.call_args_list if call.args]


class TestAxisClearedEmissionGated:
    """The new ``axis.cleared`` emission is gated by the flag."""

    def test_emits_axis_cleared_when_flag_enabled_and_entry_evicted(
        self, _reset_singletons: None
    ) -> None:
        _install_governor(causal_chain_enabled=True)
        _pre_record_rss_entry()

        with patch.object(_governor_mod, "logger", MagicMock()) as mock_logger:
            _clear_axis_entry_for_reason(_RSS_AXIS)
            events = _info_event_names(mock_logger)

        # Sibling-additive: both events fire on the same path.
        assert events.count("axis.cleared") == 1
        assert events.count("engine.resources.cohort_auto_cleared") == 1

        # Field shape of the new event — operator-readable triple.
        axis_cleared_call = next(
            call
            for call in mock_logger.info.call_args_list
            if call.args and call.args[0] == "axis.cleared"
        )
        assert axis_cleared_call.kwargs == {
            "axis": "engine_resources",
            "reason": _RSS_REASON,
            "source": "resource_cohort_governor",
        }

    def test_no_axis_cleared_when_flag_disabled(self, _reset_singletons: None) -> None:
        _install_governor(causal_chain_enabled=False)
        _pre_record_rss_entry()

        with patch.object(_governor_mod, "logger", MagicMock()) as mock_logger:
            _clear_axis_entry_for_reason(_RSS_AXIS)
            events = _info_event_names(mock_logger)

        assert "axis.cleared" not in events
        # Sibling event STILL fires — flag only gates the new emission.
        assert events.count("engine.resources.cohort_auto_cleared") == 1

    def test_no_axis_cleared_when_nothing_to_evict(self, _reset_singletons: None) -> None:
        """If the degraded store had no matching entry, ``clear_reason``
        returns False and neither event fires (existing contract)."""
        _install_governor(causal_chain_enabled=True)
        # Note: no _pre_record_rss_entry() — store is empty.

        with patch.object(_governor_mod, "logger", MagicMock()) as mock_logger:
            _clear_axis_entry_for_reason(_RSS_AXIS)
            events = _info_event_names(mock_logger)

        assert "axis.cleared" not in events
        assert "engine.resources.cohort_auto_cleared" not in events


class TestGovernorCausalChainFlagDefault:
    """The flag itself defaults to False on the dataclass."""

    def test_dataclass_default_is_false(self) -> None:
        governor = ResourceCohortGovernor()
        assert governor.causal_chain_enabled is False

    def test_from_tuning_default_is_false(self) -> None:
        from sovyx.engine.config import ObservabilityTuningConfig

        tuning = ObservabilityTuningConfig()
        governor = ResourceCohortGovernor.from_tuning(tuning)
        assert governor.causal_chain_enabled is False

    def test_from_tuning_propagates_explicit_true(self) -> None:
        from sovyx.engine.config import ObservabilityTuningConfig

        tuning = ObservabilityTuningConfig()
        governor = ResourceCohortGovernor.from_tuning(tuning, causal_chain_enabled=True)
        assert governor.causal_chain_enabled is True


class TestOX1ConfigCausalChainFlag:
    """The OX-1 namespace flag itself defaults to False."""

    def test_default_is_false(self) -> None:
        from sovyx.engine.config import OX1Config

        cfg = OX1Config()
        assert cfg.causal_chain_enabled is False

    def test_env_var_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovyx.engine.config import OX1Config

        monkeypatch.setenv("SOVYX_OX1__CAUSAL_CHAIN_ENABLED", "true")
        cfg = OX1Config()
        assert cfg.causal_chain_enabled is True
