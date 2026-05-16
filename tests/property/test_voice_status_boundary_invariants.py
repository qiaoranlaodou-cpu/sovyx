"""Hypothesis property tests for VoiceStatusResponse boundary.

Mission C2 §T1.3.a — property-tests the ``input_device`` lattice
exhaustively so any future narrowing of the union surfaces as a
deterministic Hypothesis falsifier, not as a production 500.

Mission anchor:
``docs-internal/missions/MISSION-c2-voice-status-response-contract-2026-05-16.md``
§T1.3.a.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sovyx.dashboard.routes.voice import VoiceStatusResponse

# PortAudio device index is non-negative int32 in the public API; the
# upper bound matches the platform-portable enumeration cap. The text
# strategy intentionally allows the empty string and unicode device
# names (which sounddevice surfaces verbatim from the host driver).
_input_device_strategy = st.one_of(
    st.none(),
    st.integers(min_value=0, max_value=2**31 - 1),
    st.text(min_size=0, max_size=256),
)


@given(value=_input_device_strategy)
@settings(max_examples=200, deadline=None)
def test_input_device_accepts_any_union_value(value: int | str | None) -> None:
    """For ANY (None | int | str) input, the boundary MUST validate.

    Deadline is disabled because pydantic validation cost varies
    across the unicode strategy's tail and Windows monotonic clock
    coarseness (anti-pattern #22) makes a fixed deadline brittle.
    """
    shape = {
        "pipeline": {"running": True, "state": "idle"},
        "capture": {"input_device": value},
    }
    response = VoiceStatusResponse.model_validate(shape)
    # Identity round-trip — pydantic does not coerce within the union;
    # int stays int, str stays str, None stays None.
    assert response.capture.input_device == value
    if value is None:
        assert response.capture.input_device is None
    else:
        assert type(response.capture.input_device) is type(value)


@given(
    running=st.booleans(),
    sample_rate=st.integers(min_value=0, max_value=192_000),
    frames_delivered=st.integers(min_value=0, max_value=2**31 - 1),
    last_rms_db=st.one_of(st.none(), st.floats(min_value=-120.0, max_value=0.0)),
)
@settings(max_examples=100, deadline=None)
def test_full_capture_block_invariants(
    running: bool,
    sample_rate: int,
    frames_delivered: int,
    last_rms_db: float | None,
) -> None:
    """Whole capture block round-trips cleanly for any in-range shape."""
    shape = {
        "pipeline": {"running": running, "state": "idle"},
        "capture": {
            "running": running,
            "input_device": 7,
            "sample_rate": sample_rate,
            "frames_delivered": frames_delivered,
            "last_rms_db": last_rms_db,
        },
    }
    response = VoiceStatusResponse.model_validate(shape)
    assert response.capture.running is running
    assert response.capture.sample_rate == sample_rate
    assert response.capture.frames_delivered == frames_delivered
    if last_rms_db is None:
        assert response.capture.last_rms_db is None
    else:
        assert response.capture.last_rms_db == last_rms_db
