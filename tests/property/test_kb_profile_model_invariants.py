"""Hypothesis property tests for ``KBProfileModel`` (Mission C C.6 §4).

Augments the Mission C C.6 §1 paired boundary tests in
``tests/dashboard/test_voice_kb_boundary.py`` with random-input
fuzz coverage on the field-level invariants the dashboard route
relies on:

* ``schema_version`` ``Field(ge=1, le=1)`` — current loader pins
  schema v1; any other integer MUST fail honest at the boundary
  (invariant P6 — fail honest, fail fast).
* ``profile_version`` ``Field(ge=1)`` — provenance gate.
* ``profile_id`` ``Field(pattern=r"^[a-z0-9_]+$")`` — filename-stem
  invariant relied on by the loader.
* ``match_threshold`` ``Field(default=0.6, ge=0.0, le=1.0)`` —
  cascade scorer relies on the [0.0, 1.0] cap.
* ``driver_family`` ``Literal["hda","sof","usb-audio","bt"]``.

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.6
sub-sequence step 4 (Hypothesis property tests).
"""

from __future__ import annotations

import re
from textwrap import dedent
from typing import Any

import pytest
import yaml
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from sovyx.voice.health._mixer_kb.schema import KBProfileModel

# Canonical-good YAML (mirror of tests/dashboard/test_voice_kb_boundary.py
# _CANONICAL_YAML). Kept inline so the property suite is self-contained
# and does not coupling-import the boundary test module.
_CANONICAL_YAML = dedent("""
    schema_version: 1
    profile_id: vaio_vjfe69_sn6180
    profile_version: 1
    description: Sony VAIO FE-series with Conexant SN6180.

    codec_id_glob: "14F1:5045"
    driver_family: hda
    system_vendor_glob: "Sony*"
    system_product_glob: "VJFE69*"
    kernel_major_minor_glob: "6.*"
    audio_stack: pipewire
    match_threshold: 0.6

    factory_regime: attenuation
    factory_signature:
      capture_master:
        expected_fraction_range: [0.3, 0.6]
      internal_mic_boost:
        expected_raw_range: [0, 0]

    recommended_preset:
      controls:
        - role: capture_master
          value: {fraction: 1.0}
        - role: internal_mic_boost
          value: {raw: 0}
      auto_mute_mode: disabled
      runtime_pm_target: "on"

    validation:
      rms_dbfs_range: [-30, -15]
      peak_dbfs_max: -2
      snr_db_vocal_band_min: 15
      silero_prob_min: 0.5
      wake_word_stage2_prob_min: 0.4

    verified_on:
      - system_product: "VJFE69F11X-B0221H"
        codec_id: "14F1:5045"
        kernel: "6.14.0-37"
        distro: "linuxmint-22.2"
        verified_at: "2026-04-23"
        verified_by: "sovyx-core-pilot"

    contributed_by: sovyx-core
""").strip()


_PROFILE_ID_RE = re.compile(r"^[a-z0-9_]+$")
_VALID_DRIVER_FAMILIES = ("hda", "sof", "usb-audio", "bt")


def _base_payload() -> dict[str, Any]:
    """Fresh dict per call so property mutations don't bleed across runs."""
    return yaml.safe_load(_CANONICAL_YAML)


# ── match_threshold ────────────────────────────────────────────────────


@given(value=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
@settings(max_examples=150, deadline=None)
def test_match_threshold_accepts_any_value_in_unit_interval(value: float) -> None:
    payload = _base_payload()
    payload["match_threshold"] = value
    model = KBProfileModel.model_validate(payload)
    assert model.match_threshold == value


@given(value=st.floats(allow_nan=False, allow_infinity=False))
@settings(max_examples=200, deadline=None)
def test_match_threshold_rejects_anything_outside_unit_interval(value: float) -> None:
    assume(value < 0.0 or value > 1.0)
    payload = _base_payload()
    payload["match_threshold"] = value
    with pytest.raises(ValidationError):
        KBProfileModel.model_validate(payload)


@given(value=st.one_of(st.just(float("nan")), st.just(float("inf")), st.just(float("-inf"))))
@settings(max_examples=10, deadline=None)
def test_match_threshold_rejects_nan_and_inf(value: float) -> None:
    payload = _base_payload()
    payload["match_threshold"] = value
    with pytest.raises(ValidationError):
        KBProfileModel.model_validate(payload)


# ── schema_version ─────────────────────────────────────────────────────


@given(value=st.integers(min_value=-(2**31), max_value=2**31 - 1))
@settings(max_examples=150, deadline=None)
def test_schema_version_rejects_anything_other_than_one(value: int) -> None:
    assume(value != 1)
    payload = _base_payload()
    payload["schema_version"] = value
    with pytest.raises(ValidationError):
        KBProfileModel.model_validate(payload)


def test_schema_version_accepts_exactly_one() -> None:
    payload = _base_payload()
    payload["schema_version"] = 1
    model = KBProfileModel.model_validate(payload)
    assert model.schema_version == 1


# ── profile_version ────────────────────────────────────────────────────


@given(value=st.integers(min_value=1, max_value=2**31 - 1))
@settings(max_examples=100, deadline=None)
def test_profile_version_accepts_any_positive_int(value: int) -> None:
    payload = _base_payload()
    payload["profile_version"] = value
    model = KBProfileModel.model_validate(payload)
    assert model.profile_version == value


@given(value=st.integers(min_value=-(2**31), max_value=0))
@settings(max_examples=80, deadline=None)
def test_profile_version_rejects_zero_or_negative(value: int) -> None:
    payload = _base_payload()
    payload["profile_version"] = value
    with pytest.raises(ValidationError):
        KBProfileModel.model_validate(payload)


# ── profile_id pattern ─────────────────────────────────────────────────


@given(
    value=st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"),
            max_codepoint=ord("z"),
        ),
        min_size=1,
        max_size=40,
    ),
)
@settings(max_examples=80, deadline=None)
def test_profile_id_accepts_lowercase_alpha_strings(value: str) -> None:
    """Pure lowercase alpha matches the ``^[a-z0-9_]+$`` pattern."""
    payload = _base_payload()
    payload["profile_id"] = value
    model = KBProfileModel.model_validate(payload)
    assert model.profile_id == value


@given(
    value=st.text(min_size=1, max_size=40).filter(lambda v: not _PROFILE_ID_RE.match(v)),
)
@settings(max_examples=200, deadline=None)
def test_profile_id_rejects_anything_outside_pattern(value: str) -> None:
    """Caps, dashes, dots, spaces, unicode → reject. The filename-stem
    invariant at the route layer relies on the pattern check passing
    before the equality compare."""
    payload = _base_payload()
    payload["profile_id"] = value
    with pytest.raises(ValidationError):
        KBProfileModel.model_validate(payload)


def test_profile_id_empty_string_rejected() -> None:
    """``min_length=1`` boundary."""
    payload = _base_payload()
    payload["profile_id"] = ""
    with pytest.raises(ValidationError):
        KBProfileModel.model_validate(payload)


# ── driver_family Literal ──────────────────────────────────────────────


@given(value=st.sampled_from(_VALID_DRIVER_FAMILIES))
@settings(max_examples=20, deadline=None)
def test_driver_family_accepts_every_literal_value(value: str) -> None:
    payload = _base_payload()
    payload["driver_family"] = value
    model = KBProfileModel.model_validate(payload)
    assert model.driver_family == value


@given(
    value=st.text(min_size=1, max_size=20).filter(
        lambda v: v not in _VALID_DRIVER_FAMILIES,
    ),
)
@settings(max_examples=100, deadline=None)
def test_driver_family_rejects_anything_outside_literal_set(value: str) -> None:
    payload = _base_payload()
    payload["driver_family"] = value
    with pytest.raises(ValidationError):
        KBProfileModel.model_validate(payload)
