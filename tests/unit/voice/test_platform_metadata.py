"""Unit tests for :mod:`sovyx.voice._platform_metadata` (Mission H2 §T1.6).

Verifies the PlatformAudioFamily StrEnum, the strategy-prefix → family
resolver, the majority-vote helper, and the cached platform-token
resolver.
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from sovyx.voice._platform_metadata import (
    PlatformAudioFamily,
    current_platform_token,
    is_mixed_platform_strategy_list,
    resolve_family_from_strategies,
    resolve_family_from_strategy_name,
)


class TestPlatformAudioFamilyEnum:
    """StrEnum membership invariants."""

    def test_is_str_enum(self) -> None:
        assert issubclass(PlatformAudioFamily, StrEnum)

    def test_has_eight_members(self) -> None:
        """8 families per mission spec §0."""
        assert len(list(PlatformAudioFamily)) == 8

    def test_value_strings_are_snake_case(self) -> None:
        """Every family value is snake_case + lowercase."""
        for family in PlatformAudioFamily:
            assert family.value == family.value.lower()
            assert " " not in family.value


class TestResolveFamilyFromStrategyName:
    """Strategy-prefix → family mapping."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            # Linux ALSA prefix
            ("linux.alsa_mixer_reset", PlatformAudioFamily.ALSA_CAPTURE_CHAIN),
            ("linux.alsa_capture_switch", PlatformAudioFamily.ALSA_CAPTURE_CHAIN),
            # Linux PipeWire prefix
            ("linux.pipewire_direct", PlatformAudioFamily.PIPEWIRE_FILTER_CHAIN),
            ("linux.pipewire_filter_chain_reset", PlatformAudioFamily.PIPEWIRE_FILTER_CHAIN),
            # Linux WirePlumber + session-manager prefix
            (
                "linux.wireplumber_default_source",
                PlatformAudioFamily.WIREPLUMBER_DEFAULT_SOURCE,
            ),
            (
                "linux.session_manager_escape",
                PlatformAudioFamily.WIREPLUMBER_DEFAULT_SOURCE,
            ),
            # Linux module-echo-cancel prefix
            (
                "linux.module_echo_cancel_disable",
                PlatformAudioFamily.MODULE_ECHO_CANCEL,
            ),
            # Windows voice clarity prefix
            ("win.voice_clarity_disable", PlatformAudioFamily.VOICE_CLARITY),
            ("win.wasapi_exclusive_enable", PlatformAudioFamily.VOICE_CLARITY),
            # Generic Windows fallback
            ("win.something_else", PlatformAudioFamily.VOICE_CLARITY),
            # macOS voice isolation
            (
                "darwin.voice_isolation_disable",
                PlatformAudioFamily.VOICE_ISOLATION,
            ),
            ("darwin.coreaudio_reset", PlatformAudioFamily.COREAUDIO_VOICE_PROCESSING),
            # Generic darwin fallback
            ("darwin.something_else", PlatformAudioFamily.VOICE_ISOLATION),
            # Unknown prefix
            ("freebsd.unknown_strategy", PlatformAudioFamily.NOOP),
            # Empty string
            ("", PlatformAudioFamily.NOOP),
        ],
    )
    def test_resolve_known_prefix(self, name: str, expected: PlatformAudioFamily) -> None:
        assert resolve_family_from_strategy_name(name) is expected


class TestResolveFamilyFromStrategies:
    """Majority-vote across strategy lists."""

    def test_empty_input(self) -> None:
        """Empty list maps to NOOP."""
        assert resolve_family_from_strategies([]) is PlatformAudioFamily.NOOP

    def test_single_strategy(self) -> None:
        result = resolve_family_from_strategies(["linux.alsa_mixer_reset"])
        assert result is PlatformAudioFamily.ALSA_CAPTURE_CHAIN

    def test_majority_wins(self) -> None:
        """Two-against-one majority resolves to the dominant family."""
        result = resolve_family_from_strategies(
            [
                "linux.alsa_mixer_reset",
                "linux.alsa_capture_switch",
                "linux.pipewire_direct",
            ]
        )
        assert result is PlatformAudioFamily.ALSA_CAPTURE_CHAIN

    def test_l1067_forensic_replay(self) -> None:
        """The L1067 strategy list resolves to ALSA_CAPTURE_CHAIN.

        Two ALSA strategies (alsa_mixer_reset + alsa_capture_switch)
        beat one PipeWire + one WirePlumber + one WirePlumber-sibling
        (session_manager). The ties between WirePlumber+session_manager
        do not change the majority winner.
        """
        result = resolve_family_from_strategies(
            [
                "linux.alsa_mixer_reset",
                "linux.session_manager_escape",
                "linux.pipewire_direct",
                "linux.wireplumber_default_source",
                "linux.alsa_capture_switch",
            ]
        )
        assert result is PlatformAudioFamily.ALSA_CAPTURE_CHAIN

    def test_unrecognised_only_returns_noop(self) -> None:
        """All-unknown strategies fold to NOOP."""
        result = resolve_family_from_strategies(["unknown.a", "unknown.b"])
        assert result is PlatformAudioFamily.NOOP

    def test_deterministic_ordering(self) -> None:
        """Repeated calls return the same family for the same input."""
        names = ["linux.alsa_mixer_reset", "linux.pipewire_direct"]
        first = resolve_family_from_strategies(names)
        second = resolve_family_from_strategies(names)
        third = resolve_family_from_strategies(names)
        assert first is second is third


class TestIsMixedPlatformStrategyList:
    """Mixed-platform detector for the wrapper helper's WARN trigger."""

    def test_empty_input_returns_false(self) -> None:
        assert is_mixed_platform_strategy_list([]) is False

    def test_single_strategy_returns_false(self) -> None:
        assert is_mixed_platform_strategy_list(["linux.alsa_mixer_reset"]) is False

    def test_homogeneous_linux_families_returns_false(self) -> None:
        """Linux cascade legitimately mixes families (ALSA + PipeWire +
        WirePlumber). All Linux → not mixed-platform.
        """
        assert (
            is_mixed_platform_strategy_list(
                [
                    "linux.alsa_mixer_reset",
                    "linux.alsa_capture_switch",
                    "linux.pipewire_direct",
                    "linux.wireplumber_default_source",
                    "linux.session_manager_escape",
                ]
            )
            is False
        )

    def test_homogeneous_windows_returns_false(self) -> None:
        assert (
            is_mixed_platform_strategy_list(
                [
                    "win.voice_clarity_disable",
                    "win.wasapi_exclusive_enable",
                ]
            )
            is False
        )

    def test_clear_cross_platform_mix_returns_true(self) -> None:
        """A Windows + Linux split signals a structural bug elsewhere."""
        assert (
            is_mixed_platform_strategy_list(
                [
                    "win.voice_clarity_disable",
                    "linux.alsa_mixer_reset",
                ]
            )
            is True
        )

    def test_three_platforms_returns_true(self) -> None:
        """Three distinct platforms in one cascade is clearly mixed."""
        assert (
            is_mixed_platform_strategy_list(
                [
                    "win.voice_clarity_disable",
                    "linux.alsa_mixer_reset",
                    "darwin.voice_isolation_disable",
                ]
            )
            is True
        )

    def test_unrecognised_prefixes_only_returns_false(self) -> None:
        """Strategies without recognised platform prefixes aren't
        cross-platform — they're just unknown.
        """
        assert is_mixed_platform_strategy_list(["unknown.foo", "unknown.bar"]) is False


class TestCurrentPlatformToken:
    """sys.platform → token resolver with cache discipline."""

    def setup_method(self) -> None:
        """Clear the @functools.cache before each test so monkeypatch works."""
        current_platform_token.cache_clear()

    def teardown_method(self) -> None:
        current_platform_token.cache_clear()

    def test_returns_one_of_four_tokens(self) -> None:
        result = current_platform_token()
        assert result in {"linux", "windows", "darwin", "other"}

    def test_resolves_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "linux")
        current_platform_token.cache_clear()
        assert current_platform_token() == "linux"

    def test_resolves_linux2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Legacy ``linux2`` token must also resolve to ``linux`` per the
        ``startswith`` check."""
        monkeypatch.setattr("sys.platform", "linux2")
        current_platform_token.cache_clear()
        assert current_platform_token() == "linux"

    def test_resolves_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "win32")
        current_platform_token.cache_clear()
        assert current_platform_token() == "windows"

    def test_resolves_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "darwin")
        current_platform_token.cache_clear()
        assert current_platform_token() == "darwin"

    def test_resolves_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.platform", "freebsd14")
        current_platform_token.cache_clear()
        assert current_platform_token() == "other"
