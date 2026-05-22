"""Boundary round-trip tests for ``KBProfileModel`` at the
``/api/voice/kb/validate`` route boundary (Mission C C.6 §1 follow-up).

Mission anchor:
``docs-internal/MISSION-C-REMEDIATION-PLAN-2026-05-21.md`` §3 Phase C.6
sub-sequence step 1 (paired-test landings).

The dashboard route ``post_voice_kb_validate`` at
``src/sovyx/dashboard/routes/voice_kb.py:252`` runs:

    parsed = yaml.safe_load(payload.yaml_body)
    model = KBProfileModel.model_validate(parsed)

— the canonical YAML-boundary anti-pattern #40 surface. The model
itself lives at ``src/sovyx/voice/health/_mixer_kb/schema.py:302``;
rich validation-rule coverage already exists at
``tests/unit/voice/health/test_mixer_kb.py``. The gate (
``check_boundary_round_trip_coverage`` Gate 8) discovers paired tests
under ``tests/dashboard/`` and ``tests/integration/dashboard/`` only,
so this file is the named-anchor pairing for the *dashboard route*
boundary specifically — it mirrors the producer's runtime flow
(YAML text → ``yaml.safe_load`` → ``KBProfileModel.model_validate``)
rather than reaching into the schema test cohort.

Quality Gate 8 sees the ``KBProfileModel.model_validate(...)`` call
sites here and pairs them against the route's boundary call.
"""

from __future__ import annotations

from textwrap import dedent

import pytest
import yaml
from pydantic import ValidationError

from sovyx.voice.health._mixer_kb.schema import KBProfileModel

# Canonical-good YAML — mirrors the v0.43.x pilot profile shipped
# under ``src/sovyx/voice/health/_mixer_kb/profiles/`` (Sony VAIO +
# Conexant SN6180). Keeps the test self-contained so a producer-side
# rename surfaces here loudly, not via the sibling unit test cohort.
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


def _producer_payload() -> dict[str, object]:
    """Mirror ``post_voice_kb_validate`` runtime: YAML text round-trips
    through ``yaml.safe_load`` before reaching the typed boundary.
    """
    return yaml.safe_load(_CANONICAL_YAML)


class TestKBProfileModelBoundaryRoundTrip:
    """``KBProfileModel.model_validate(...)`` — anti-pattern #40 closure
    for ``/api/voice/kb/validate``.
    """

    def test_canonical_yaml_round_trips(self) -> None:
        """The pilot profile YAML — what the dashboard route would
        accept from a contributor's PR upload — round-trips cleanly.
        """
        payload = _producer_payload()
        model = KBProfileModel.model_validate(payload)
        assert model.profile_id == "vaio_vjfe69_sn6180"
        assert model.profile_version == 1
        assert model.schema_version == 1
        assert model.driver_family == "hda"
        assert model.factory_regime == "attenuation"
        assert model.match_threshold == 0.6
        # Factory-signature roles validated via field_validator.
        assert set(model.factory_signature) == {"capture_master", "internal_mic_boost"}
        # verified_on is tuple-typed in the model; min_length=1 enforced.
        assert len(model.verified_on) == 1
        assert model.verified_on[0].verified_by == "sovyx-core-pilot"

    def test_unknown_top_level_field_rejected(self) -> None:
        """``model_config = _STRICT_CONFIG`` rejects unknown keys —
        keeps the YAML schema honest at the dashboard boundary."""
        payload = _producer_payload()
        payload["mystery_field"] = "value"
        with pytest.raises(ValidationError, match="mystery_field"):
            KBProfileModel.model_validate(payload)

    def test_missing_required_field_rejected(self) -> None:
        """Removing ``codec_id_glob`` (required match-criterion) MUST
        surface as a ValidationError so the route returns 4xx instead
        of crashing the loader downstream."""
        payload = _producer_payload()
        del payload["codec_id_glob"]
        with pytest.raises(ValidationError, match="codec_id_glob"):
            KBProfileModel.model_validate(payload)

    def test_invalid_driver_family_rejected(self) -> None:
        """``driver_family`` is a ``Literal["hda", "sof", "usb-audio",
        "bt"]`` — anything else MUST fail at the boundary."""
        payload = _producer_payload()
        payload["driver_family"] = "firewire"
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(payload)

    def test_schema_version_above_supported_rejected(self) -> None:
        """``schema_version`` is bounded ``ge=1, le=1`` — a v2 YAML
        MUST fail honest (invariant P6 "fail honest, fail fast") so
        the loader does not silently degrade."""
        payload = _producer_payload()
        payload["schema_version"] = 2
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(payload)

    def test_match_threshold_below_zero_rejected(self) -> None:
        """``match_threshold`` is bounded ``[0.0, 1.0]``."""
        payload = _producer_payload()
        payload["match_threshold"] = -0.1
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(payload)

    def test_match_threshold_above_one_rejected(self) -> None:
        payload = _producer_payload()
        payload["match_threshold"] = 1.1
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(payload)

    def test_factory_signature_empty_rejected(self) -> None:
        """``factory_signature`` carries ``Field(min_length=1)`` — an
        empty dict is a definitional bug (no factory regime to match)."""
        payload = _producer_payload()
        payload["factory_signature"] = {}
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(payload)

    def test_unknown_factory_signature_role_rejected(self) -> None:
        """``_factory_signature_roles_known`` validator catches role
        names that don't map to ``MixerControlRole`` enum members."""
        payload = _producer_payload()
        payload["factory_signature"]["bogus_role"] = {"expected_raw_range": [0, 0]}
        with pytest.raises(ValidationError, match="bogus_role"):
            KBProfileModel.model_validate(payload)

    def test_profile_id_pattern_rejected(self) -> None:
        """``profile_id`` is ``Field(pattern=r"^[a-z0-9_]+$")`` — caps
        and dashes (PR-style naming) MUST fail so filename-stem
        invariant at the route layer can rely on the pydantic check."""
        payload = _producer_payload()
        payload["profile_id"] = "VAIO-vjfe69"
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(payload)

    def test_verified_on_empty_tuple_rejected(self) -> None:
        """``verified_on`` carries ``min_length=1`` — a profile MUST
        carry at least one verification record before PR-merge."""
        payload = _producer_payload()
        payload["verified_on"] = []
        with pytest.raises(ValidationError):
            KBProfileModel.model_validate(payload)
