"""Hypothesis property tests for the dual-emission wrapper (Mission H2 §T1.6).

Six invariants the wrapper must hold for any valid input.

The tests deliberately avoid caplog-based assertion because pytest's
``caplog`` fixture is function-scoped while Hypothesis re-invokes the
test body across many examples — fixture-sharing semantics under full-
suite test ordering can shadow caplog records that ARE captured in
isolated runs. The unit tests at :mod:`tests.unit.voice.pipeline.test_capture_integrity_emit`
verify the emission contract via caplog at the example level (3
caplog-driven cases per ``CaptureIntegrityEvent``); the property tests
here verify the ALGEBRAIC invariants that don't depend on log capture.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.voice._event_names import LEGACY_TWIN_MAP, CaptureIntegrityEvent
from sovyx.voice._platform_metadata import (
    PlatformAudioFamily,
    current_platform_token,
    is_mixed_platform_strategy_list,
    resolve_family_from_strategies,
    resolve_family_from_strategy_name,
)
from sovyx.voice.pipeline._capture_integrity_emit import (
    SCHEMA_VERSION,
    emit_capture_integrity_event,
)

_KNOWN_PREFIXES = [
    "linux.alsa_",
    "linux.pipewire_",
    "linux.wireplumber_",
    "linux.session_manager_",
    "linux.module_echo_cancel_",
    "win.voice_clarity_",
    "win.wasapi_exclusive_",
    "darwin.voice_isolation_",
    "darwin.coreaudio_",
    "unknown.",
]


_strategy_name_strategy = st.builds(
    lambda prefix, suffix: prefix + suffix,
    st.sampled_from(_KNOWN_PREFIXES),
    st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"), max_codepoint=ord("z"), whitelist_characters="_0123456789"
        ),
        min_size=1,
        max_size=20,
    ),
)


@pytest.fixture(autouse=True)
def _clear_platform_cache() -> None:
    current_platform_token.cache_clear()
    yield
    current_platform_token.cache_clear()


@given(strategies=st.lists(_strategy_name_strategy, min_size=0, max_size=10))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_invariant_family_resolution_total(strategies: list[str]) -> None:
    """For ANY valid strategy list (including empty), the family
    resolver returns a valid :class:`PlatformAudioFamily` member.
    """
    result = resolve_family_from_strategies(strategies)
    assert isinstance(result, PlatformAudioFamily)


@given(strategies=st.lists(_strategy_name_strategy, min_size=0, max_size=10))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_invariant_resolver_deterministic(strategies: list[str]) -> None:
    """Repeated calls with the same input return the same family."""
    first = resolve_family_from_strategies(strategies)
    second = resolve_family_from_strategies(strategies)
    assert first is second


@given(
    event=st.sampled_from(list(CaptureIntegrityEvent)),
    strategies=st.lists(_strategy_name_strategy, min_size=0, max_size=5),
    mind_id=st.text(min_size=1, max_size=20),
)
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_invariant_wrapper_never_raises(
    event: CaptureIntegrityEvent,
    strategies: list[str],
    mind_id: str,
) -> None:
    """For ANY valid input, the wrapper does not raise."""
    emit_capture_integrity_event(
        event,
        "error",
        mind_id=mind_id,
        strategies=strategies,
        voice_clarity_active=False,
    )


@given(name=_strategy_name_strategy)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_invariant_single_strategy_resolution(name: str) -> None:
    """For any single recognised-prefix strategy, the family resolver
    returns the same value as the multi-strategy resolver applied to a
    one-element list.
    """
    single = resolve_family_from_strategy_name(name)
    multi = resolve_family_from_strategies([name])
    assert single is multi


@given(strategies=st.lists(_strategy_name_strategy, min_size=0, max_size=10))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_invariant_mixed_platform_detector_total(strategies: list[str]) -> None:
    """The mixed-platform detector never raises and always returns bool."""
    result = is_mixed_platform_strategy_list(strategies)
    assert isinstance(result, bool)


@given(event=st.sampled_from(list(CaptureIntegrityEvent)))
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_invariant_legacy_twin_lookup_total(event: CaptureIntegrityEvent) -> None:
    """For every neutral :class:`CaptureIntegrityEvent`, the legacy twin
    is a non-empty string distinct from the neutral name.
    """
    legacy = LEGACY_TWIN_MAP[event]
    assert isinstance(legacy, str)
    assert len(legacy) > 0
    assert legacy != str(event)
    # The schema-version constant is locked at 2.0.0 — independent
    # invariant verifiable per example for free.
    assert SCHEMA_VERSION == "2.0.0"
