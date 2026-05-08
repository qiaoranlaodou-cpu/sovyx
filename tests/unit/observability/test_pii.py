"""Tests for sovyx.observability.pii — PIIRedactor + regex sweep + property tests.

All PII fixtures are synthetic — generated for these tests with
RFC 5737 documentation IPs, ``.test`` / ``.invalid`` reserved TLDs,
and the universally-published test credit-card numbers. Match what
``scripts/check_test_pii.py`` accepts so the CI gate stays green.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sovyx.engine.config import ObservabilityPIIConfig
from sovyx.observability.pii import (
    API_KEY_RE,
    CNPJ_RE,
    CPF_RE,
    EMAIL_RE,
    IPV4_RE,
    JWT_RE,
    PIIRedactor,
)

# ── Synthetic PII fixtures (mirror scripts/check_test_pii.py) ───────────

_FAKE_EMAIL = "synthetic.user@example-fake.test"
_FAKE_CPF = "529.982.247-25"  # well-formed test CPF
_FAKE_CNPJ = "00.000.000/0001-91"
_FAKE_PHONE_BR = "(11) 98765-4321"
_FAKE_PHONE_E164 = "+44 7700900123"  # Ofcom drama-reserved range
_FAKE_IPV4 = "203.0.113.45"  # RFC 5737 documentation block
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiJ0ZXN0LXN1YmplY3QiLCJpYXQiOjE3MDAwMDAwMDB9"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
_FAKE_API_KEY_ANTHROPIC = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_API_KEY_OPENAI = "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_API_KEY_STRIPE = "sk_live_-PLACEHOLDER-FIXTURE-FOR-TESTS-0"
_FAKE_VISA_TEST = "4111 1111 1111 1111"  # universally-published Luhn-valid test card


def _redactor(**overrides: str) -> PIIRedactor:
    """Build a PIIRedactor with default modes plus optional overrides."""
    config = ObservabilityPIIConfig(**overrides)  # type: ignore[arg-type]
    return PIIRedactor(config)


def _call(
    redactor: PIIRedactor,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    return dict(redactor(structlog.get_logger(), "info", event_dict))


# ── Per-pattern regex coverage ─────────────────────────────────────────


class TestRegexPatterns:
    """Each compiled pattern must catch its own canonical synthetic value."""

    def test_email_re_matches_fake_email(self) -> None:
        assert EMAIL_RE.search(_FAKE_EMAIL)

    def test_cpf_re_matches_fake_cpf(self) -> None:
        assert CPF_RE.search(_FAKE_CPF)

    def test_cnpj_re_matches_fake_cnpj(self) -> None:
        assert CNPJ_RE.search(_FAKE_CNPJ)

    def test_ipv4_re_matches_documentation_block(self) -> None:
        assert IPV4_RE.search(_FAKE_IPV4)

    def test_jwt_re_matches_three_segment_token(self) -> None:
        assert JWT_RE.search(_FAKE_JWT)

    def test_api_key_re_matches_anthropic_format(self) -> None:
        assert API_KEY_RE.search(_FAKE_API_KEY_ANTHROPIC)

    def test_api_key_re_matches_openai_format(self) -> None:
        assert API_KEY_RE.search(_FAKE_API_KEY_OPENAI)

    def test_api_key_re_matches_stripe_format(self) -> None:
        assert API_KEY_RE.search(_FAKE_API_KEY_STRIPE)

    def test_api_key_re_skips_short_sk_prefix(self) -> None:
        # CSS classes like "sk-button" must NOT trigger.
        assert not API_KEY_RE.search("sk-button")


# ── Per-class verbosity table ───────────────────────────────────────────


class TestVerbosityModes:
    """Each verbosity mode produces the expected projection of the value."""

    def test_minimal_drops_value_completely(self) -> None:
        red = _redactor(user_messages="minimal")
        out = _call(red, {"user_message": _FAKE_EMAIL})
        assert out["user_message"] == "[redacted]"

    def test_redacted_pattern_masks_inside_value(self) -> None:
        red = _redactor(user_messages="redacted")
        out = _call(red, {"user_message": f"contact {_FAKE_EMAIL} please"})
        assert out["user_message"] == "contact [redacted-email] please"

    def test_hashed_replaces_with_sha256_prefix(self) -> None:
        red = _redactor(emails="hashed")
        out = _call(red, {"email": _FAKE_EMAIL})
        assert out["email"].startswith("sha256:")
        assert len(out["email"]) == len("sha256:") + 12

    def test_hashed_is_deterministic(self) -> None:
        red = _redactor(emails="hashed")
        first = _call(red, {"email": _FAKE_EMAIL})["email"]
        second = _call(red, {"email": _FAKE_EMAIL})["email"]
        assert first == second

    def test_hashed_different_inputs_produce_different_hashes(self) -> None:
        red = _redactor(emails="hashed")
        out_a = _call(red, {"email": _FAKE_EMAIL})["email"]
        out_b = _call(red, {"email": "other.user@example-fake.test"})["email"]
        assert out_a != out_b

    def test_full_mode_passes_value_through(self) -> None:
        red = _redactor(prompts="full")
        out = _call(red, {"prompt": _FAKE_EMAIL})
        assert out["prompt"] == _FAKE_EMAIL


# ── Global regex sweep on free-form fields ─────────────────────────────


class TestGlobalSweep:
    """Free-form fields (not in the verbosity table) get the regex sweep."""

    def test_sweeps_email_in_arbitrary_field(self) -> None:
        red = _redactor()
        out = _call(red, {"narrative": f"sent to {_FAKE_EMAIL}"})
        assert "[redacted-email]" in out["narrative"]
        assert _FAKE_EMAIL not in out["narrative"]

    def test_sweeps_cpf(self) -> None:
        red = _redactor()
        out = _call(red, {"detail": f"client cpf {_FAKE_CPF}"})
        assert "[redacted-cpf]" in out["detail"]

    def test_sweeps_cnpj(self) -> None:
        red = _redactor()
        out = _call(red, {"detail": f"company {_FAKE_CNPJ}"})
        assert "[redacted-cnpj]" in out["detail"]

    def test_sweeps_ipv4(self) -> None:
        red = _redactor()
        out = _call(red, {"trace": f"from ip {_FAKE_IPV4}"})
        assert "[redacted-ipv4]" in out["trace"]

    def test_sweeps_jwt(self) -> None:
        red = _redactor()
        out = _call(red, {"trace": f"bearer {_FAKE_JWT}"})
        assert "[redacted-jwt]" in out["trace"]
        assert "eyJ" not in out["trace"]

    def test_sweeps_api_key(self) -> None:
        red = _redactor()
        out = _call(red, {"trace": f"key={_FAKE_API_KEY_ANTHROPIC}"})
        assert "[redacted-api-key]" in out["trace"]

    def test_sweeps_phone_br(self) -> None:
        red = _redactor()
        out = _call(red, {"detail": f"call {_FAKE_PHONE_BR}"})
        assert "[redacted-phone]" in out["detail"]

    def test_sweeps_phone_e164(self) -> None:
        red = _redactor()
        out = _call(red, {"detail": f"call {_FAKE_PHONE_E164}"})
        assert "[redacted-phone]" in out["detail"]


# ── Credit-card masking is Luhn-gated ──────────────────────────────────


class TestCreditCardMasking:
    """Card-shaped sequences only redact when they pass Luhn."""

    def test_luhn_valid_card_is_masked(self) -> None:
        red = _redactor()
        out = _call(red, {"detail": f"card {_FAKE_VISA_TEST} declined"})
        assert "[redacted-card]" in out["detail"]

    def test_luhn_invalid_card_passes_through(self) -> None:
        # Same shape, fails Luhn — must NOT be redacted as a card.
        red = _redactor()
        invalid = "1234 5678 9012 3456"
        out = _call(red, {"detail": f"order {invalid}"})
        assert "[redacted-card]" not in out["detail"]


# ── Protected envelope keys are never touched ──────────────────────────


class TestProtectedKeys:
    """Envelope / routing fields must survive every pass."""

    @pytest.mark.parametrize(
        "key",
        [
            "timestamp",
            "level",
            "logger",
            "event",
            "schema_version",
            "process_id",
            "host",
            "sovyx_version",
            "sequence_no",
            "saga_id",
            "cause_id",
            "span_id",
            "trace_id",
        ],
    )
    def test_protected_key_is_never_redacted(self, key: str) -> None:
        red = _redactor()
        # An IPv4 in the value would normally be swept — but the key
        # is on the protected list, so the value is preserved intact.
        out = _call(red, {key: _FAKE_IPV4})
        assert out[key] == _FAKE_IPV4


class TestHashRedactKeys:
    """v0.31.6 T3.6 — hardware-fingerprint fields are HASHED, not passed through.

    Operator's mic/host-api labels (e.g. ``"Razer BlackShark V2 Pro"``)
    don't match any PII regex, so the global sweep would leak them
    verbatim. The redactor's HASH layer replaces the value with a
    deterministic 12-hex-prefix so dashboards can correlate device
    occurrences without exposing the device label.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "voice_input_device_name",
            "voice_input_device_host_api",
            "voice.input_device_name",
            "voice.input_device_host_api",
        ],
    )
    def test_hardware_fingerprint_is_hashed(self, key: str) -> None:
        red = _redactor()
        out = _call(red, {key: "Razer BlackShark V2 Pro"})
        # Replaced with the canonical sha256 prefix shape, never the raw label.
        assert out[key] != "Razer BlackShark V2 Pro"
        assert isinstance(out[key], str)
        assert out[key].startswith("sha256:")
        assert len(out[key]) == len("sha256:") + 12  # noqa: PLR2004

    def test_hash_is_deterministic_across_calls(self) -> None:
        red = _redactor()
        first = _call(red, {"voice_input_device_name": "Internal Microphone"})
        second = _call(red, {"voice_input_device_name": "Internal Microphone"})
        assert first["voice_input_device_name"] == second["voice_input_device_name"]

    def test_distinct_device_names_hash_differently(self) -> None:
        red = _redactor()
        out_a = _call(red, {"voice_input_device_name": "Razer BlackShark V2 Pro"})
        out_b = _call(red, {"voice_input_device_name": "Internal Microphone"})
        assert out_a["voice_input_device_name"] != out_b["voice_input_device_name"]

    def test_pre_hashed_keys_still_pass_through(self) -> None:
        # Ensure the new HASH layer didn't regress the existing
        # pass-through allowlist. ``mind_id_hash`` / ``profile_id_hash``
        # are pre-hashed by the caller and must arrive verbatim.
        red = _redactor()
        out = _call(
            red,
            {
                "mind_id_hash": "sha256:abcdef012345",
                "profile_id_hash": "sha256:0123456789ab",
            },
        )
        assert out["mind_id_hash"] == "sha256:abcdef012345"
        assert out["profile_id_hash"] == "sha256:0123456789ab"


class TestNestedContainerRedaction:
    """v0.31.7 T2.3 — recursive walk + sibling hardware-fingerprint keys.

    Round-2 audit of v0.31.6 found two compounding gaps in the
    pre-T2.3 redactor:

    1. The redactor early-returned on every non-string value, so
       nested dicts / lists bypassed the entire pipeline (hash redact
       set + global regex sweep).
    2. The same fingerprint flowed under sibling keys
       (``endpoint_name`` / ``voice.active_endpoint_name`` /
       ``device_interface_name`` etc.) that weren't in the v0.31.6
       hash-redact set.

    These tests are the regression contract for both fixes.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "endpoint_name",
            "voice.active_endpoint_name",
            "voice.resolved_name",
            "device_interface_name",
            "friendly_name",
            "active_device_name",
            "endpoint_id",
            "voice.active_endpoint_id",
            "device_name",
            "device_uid",
            "endpoint",
        ],
    )
    def test_sibling_hardware_keys_hashed_at_top_level(self, key: str) -> None:
        red = _redactor()
        out = _call(red, {key: "Razer BlackShark V2 Pro"})
        assert out[key] != "Razer BlackShark V2 Pro"
        assert isinstance(out[key], str)
        assert out[key].startswith("sha256:")

    def test_nested_dict_endpoint_name_redacted(self) -> None:
        """Inner ``voice.active_endpoint_name`` inside a dict value is hashed."""
        red = _redactor()
        out = _call(
            red,
            {
                "audio.apo.scan": {
                    "voice.active_endpoint_name": "Microphone (Razer BlackShark V2 Pro)",
                    "voice.endpoint_count": 1,
                },
            },
        )
        inner = out["audio.apo.scan"]
        assert isinstance(inner, dict)
        assert inner["voice.active_endpoint_name"] != ("Microphone (Razer BlackShark V2 Pro)")
        assert inner["voice.active_endpoint_name"].startswith("sha256:")
        # Non-fingerprint keys survive unchanged.
        assert inner["voice.endpoint_count"] == 1

    def test_nested_list_endpoints_hashed_as_whole_payload(self) -> None:
        """``voice.endpoints`` list at top level is hashed as a single sentinel."""
        red = _redactor()
        endpoints = [
            {"endpoint_name": "Razer BlackShark V2 Pro", "fx_binding_count": 3},
            {"endpoint_name": "Internal Microphone", "fx_binding_count": 0},
        ]
        out = _call(red, {"voice.endpoints": endpoints})
        # The whole list value collapses to a hash sentinel string.
        assert isinstance(out["voice.endpoints"], str)
        assert out["voice.endpoints"].startswith("sha256:")
        # And the raw device names never reached the output.
        assert "Razer" not in out["voice.endpoints"]
        assert "Internal" not in out["voice.endpoints"]

    def test_voice_endpoints_whole_payload_hash_is_deterministic(self) -> None:
        """Same payload → same sentinel (correlation across log lines)."""
        red = _redactor()
        endpoints = [{"endpoint_name": "Razer BlackShark V2 Pro"}]
        first = _call(red, {"voice.endpoints": list(endpoints)})
        second = _call(red, {"voice.endpoints": list(endpoints)})
        assert first["voice.endpoints"] == second["voice.endpoints"]

    def test_nested_list_under_neutral_key_recurses_into_inner_dict(self) -> None:
        """A list under a NON-redact key still walks each item's inner keys."""
        red = _redactor()
        # ``per_endpoint_detail`` is not a redact key, so the list passes
        # through item-by-item; each inner dict's ``endpoint_name`` is
        # hashed because the redact set catches the inner key.
        out = _call(
            red,
            {
                "per_endpoint_detail": [
                    {"endpoint_name": "Razer BlackShark V2 Pro", "rank": 1},
                    {"endpoint_name": "Internal Microphone", "rank": 2},
                ],
            },
        )
        items = out["per_endpoint_detail"]
        assert isinstance(items, list)
        for item in items:
            assert item["endpoint_name"].startswith("sha256:")
        assert items[0]["rank"] == 1

    def test_existing_redact_keys_still_redacted_after_split(self) -> None:
        """v0.31.6 T3.6 keys still hash — sanity check for no regression."""
        red = _redactor()
        out = _call(red, {"voice_input_device_name": "Razer BlackShark V2 Pro"})
        assert out["voice_input_device_name"].startswith("sha256:")

    def test_pre_hashed_forms_pass_through(self) -> None:
        """Pre-hashed keys (``mind_id_hash`` etc.) survive the recursive walk."""
        red = _redactor()
        out = _call(
            red,
            {
                "mind_id_hash": "abc123def456",
                "profile_id_hash": "0123456789ab",
                "fingerprint_hash": "deadbeefcafe",
            },
        )
        assert out["mind_id_hash"] == "abc123def456"
        assert out["profile_id_hash"] == "0123456789ab"
        assert out["fingerprint_hash"] == "deadbeefcafe"

    def test_recursive_walk_terminates_on_circular_ref(self) -> None:
        """A self-referential dict must not blow the stack."""
        red = _redactor()
        cyclic: dict[str, Any] = {"self_ref_marker": "outer"}
        cyclic["self"] = cyclic  # type: ignore[assignment]
        # Must NOT raise RecursionError. The depth guard kicks in
        # before Python's recursion limit; the deepest reachable
        # node becomes a sentinel.
        out = _call(red, {"audit": cyclic})
        # Outer marker is still reachable + redacted (no PII regex
        # match → string passes through the global sweep unchanged).
        assert out["audit"]["self_ref_marker"] == "outer"

    def test_recursive_walk_terminates_on_circular_list(self) -> None:
        """A list containing itself must not blow the stack."""
        red = _redactor()
        cyclic_list: list[Any] = ["sentinel-marker"]
        cyclic_list.append(cyclic_list)
        out = _call(red, {"audit": cyclic_list})
        # First element is still a string (regex-swept).
        assert out["audit"][0] == "sentinel-marker"

    def test_recursive_walk_no_op_on_event_without_redact_keys(self) -> None:
        """Events with only safe primitives don't recurse — strings get swept,
        non-strings return as-is.
        """
        red = _redactor()
        out = _call(
            red,
            {
                "count": 42,
                "flag": True,
                "label": "no-pii-here",
                "missing": None,
            },
        )
        assert out["count"] == 42
        assert out["flag"] is True
        assert out["label"] == "no-pii-here"
        assert out["missing"] is None

    def test_deeply_nested_dict_replaces_with_sentinel_at_max_depth(self) -> None:
        """Beyond ``_MAX_RECURSION_DEPTH`` the value is replaced with sentinel."""
        red = _redactor()
        # Build depth-12 nested dict — exceeds depth 8 limit.
        deep: dict[str, Any] = {"leaf": "deep-value"}
        for _ in range(12):
            deep = {"wrap": deep}
        out = _call(red, {"root": deep})
        # Walk down until we hit the sentinel.
        cursor: Any = out["root"]
        for _ in range(12):
            if cursor == "[redacted-depth-exceeded]":
                break
            cursor = cursor["wrap"] if isinstance(cursor, dict) else cursor
        assert cursor == "[redacted-depth-exceeded]"

    def test_tuple_value_redacted_recursively(self) -> None:
        """Tuple containing a fingerprint key in a sub-dict gets walked."""
        red = _redactor()
        payload = ({"endpoint_name": "Razer BlackShark V2 Pro"},)
        out = _call(red, {"voice.endpoint_tuple": payload})
        result = out["voice.endpoint_tuple"]
        assert isinstance(result, tuple)
        inner = result[0]
        assert inner["endpoint_name"].startswith("sha256:")


# ── Idempotence ────────────────────────────────────────────────────────


class TestIdempotence:
    """A redacted record passed through the redactor again must not change."""

    def test_double_redaction_is_stable(self) -> None:
        red = _redactor()
        once = _call(red, {"narrative": f"to {_FAKE_EMAIL} from {_FAKE_IPV4}"})
        twice = _call(red, dict(once))
        assert once == twice

    def test_protected_keys_idempotent_with_redaction(self) -> None:
        red = _redactor()
        once = _call(
            red,
            {"event": "sample.event", "narrative": f"see {_FAKE_EMAIL}"},
        )
        twice = _call(red, dict(once))
        assert twice["event"] == "sample.event"
        assert twice["narrative"] == once["narrative"]


# ── Non-string values pass through untouched ───────────────────────────


class TestNonStringValues:
    """ints / bools / dicts / Nones must survive the processor."""

    def test_int_value_unchanged(self) -> None:
        red = _redactor()
        out = _call(red, {"count": 42})
        assert out["count"] == 42

    def test_bool_value_unchanged(self) -> None:
        red = _redactor()
        out = _call(red, {"flag": True})
        assert out["flag"] is True

    def test_none_value_unchanged(self) -> None:
        red = _redactor()
        out = _call(red, {"missing": None})
        assert out["missing"] is None


# ── Hypothesis property tests ──────────────────────────────────────────


class TestProperties:
    """Properties that must hold for arbitrary input."""

    @given(
        prefix=st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd", "Zs"),
            ),
            min_size=0,
            max_size=20,
        ),
        suffix=st.text(
            alphabet=st.characters(
                whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd", "Zs"),
            ),
            min_size=0,
            max_size=20,
        ),
    )
    @settings(
        max_examples=40,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_email_always_redacted_in_arbitrary_context(
        self,
        prefix: str,
        suffix: str,
    ) -> None:
        """Wherever the synthetic email lands inside a string, it gets masked."""
        red = _redactor()
        text = f"{prefix} {_FAKE_EMAIL} {suffix}"
        out = _call(red, {"detail": text})
        assert _FAKE_EMAIL not in out["detail"]
        assert "[redacted-email]" in out["detail"]

    @given(
        salt=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=1,
            max_size=10,
        ),
    )
    @settings(
        max_examples=30,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_hashed_mode_collision_resistance(self, salt: str) -> None:
        """Distinct inputs produce distinct sha256 prefixes (no trivial collisions)."""
        red = _redactor(emails="hashed")
        a = _call(red, {"email": f"a-{salt}@example-fake.test"})["email"]
        b = _call(red, {"email": f"b-{salt}@example-fake.test"})["email"]
        assert a != b
        assert a.startswith("sha256:")
        assert b.startswith("sha256:")

    @given(
        n=st.integers(min_value=2, max_value=6),
    )
    @settings(
        max_examples=20,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_idempotence_for_n_passes(self, n: int) -> None:
        """N-fold redaction equals single redaction (no oscillation)."""
        red = _redactor()
        record = {
            "narrative": (f"to {_FAKE_EMAIL} from {_FAKE_IPV4} with cpf {_FAKE_CPF}"),
        }
        first = _call(red, dict(record))
        current = dict(first)
        for _ in range(n):
            current = _call(red, dict(current))
        assert current == first
