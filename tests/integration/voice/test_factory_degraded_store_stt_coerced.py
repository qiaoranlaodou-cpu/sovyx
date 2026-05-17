"""Integration test — voice factory's STT-language-coerced wire shim
populates EngineDegradedStore.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.3 + §9.2.

The wire shim at ``voice/factory/_validate.py:542`` ALSO writes to the
store when MoonshineSTT's supported-language list does NOT contain the
operator's mind language (e.g. ``pt``, ``de``, ``fr``). This test
exercises the path by calling ``_create_stt(language="pt")`` and
asserting the store entry lands with the requested_language captured
in metadata.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import _patch, patch

import pytest

from sovyx.engine._degraded_store import (
    get_default_degraded_store,
    reset_default_degraded_store,
)


@pytest.fixture(autouse=True)
def _reset_store() -> Generator[None, None, None]:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


class _MoonshineSTTStub:
    """Sub-MoonshineConfig stub kept here to avoid loading the ONNX
    model in this integration test. The factory _create_stt path
    constructs MoonshineSTT(config=MoonshineConfig(language=...)) but
    we mock the constructor so the test stays hermetic."""

    def __init__(self, config: Any = None) -> None:  # noqa: ANN401
        self.config = config


def _patch_moonshine_stubs() -> tuple[_patch[Any], _patch[Any]]:
    """Anti-pattern #38 — patch the lazy import's SOURCE module
    (``sovyx.voice.stt``), NOT ``_validate`` (the lazy ``from sovyx.voice.stt
    import MoonshineSTT`` inside ``_create_stt`` resolves at the
    source module at call time)."""
    import sovyx.voice.stt as stt_module

    return (
        patch.object(stt_module, "MoonshineSTT", _MoonshineSTTStub),
        patch.object(stt_module, "MoonshineConfig", lambda **kw: kw),
    )


class TestFactoryDegradedStoreSttCoerced:
    def test_pt_coerced_to_en_records_store_entry(self) -> None:
        from sovyx.voice.factory import _validate

        stt_patch, cfg_patch = _patch_moonshine_stubs()
        with stt_patch, cfg_patch:
            _validate._create_stt(language="pt")

        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.axis == "stt"
        assert entry.reason == "stt_language_coerced"
        assert entry.metadata["requested_language"] == "pt"
        assert entry.metadata["coerced_language"] == "en"
        assert entry.metadata["engine"] == "moonshine"

    def test_supported_language_no_store_entry(self) -> None:
        """Mission C4 §16 synergy — when the language IS supported,
        the store entry is NOT recorded (the WARN log path is never
        hit). Asserts the wire shim is gated by the SAME branch the
        WARN log is gated by."""
        from sovyx.voice.factory import _validate

        stt_patch, cfg_patch = _patch_moonshine_stubs()
        with stt_patch, cfg_patch:
            _validate._create_stt(language="en")

        entries = get_default_degraded_store().snapshot()
        assert entries == []

    def test_severity_is_warn_not_error(self) -> None:
        """STT coercion is operator-actionable (install multilingual
        engine) but the voice subsystem still works in English. Phase
        1.A specs severity=warn so the composite banner stays at warn
        until a second axis joins (per ADR-D6)."""
        from sovyx.voice.factory import _validate

        stt_patch, cfg_patch = _patch_moonshine_stubs()
        with stt_patch, cfg_patch:
            _validate._create_stt(language="fr")

        entries = get_default_degraded_store().snapshot()
        assert entries[0].severity == "warn"

    def test_action_chip_points_to_settings_voice(self) -> None:
        from sovyx.voice.factory import _validate

        stt_patch, cfg_patch = _patch_moonshine_stubs()
        with stt_patch, cfg_patch:
            _validate._create_stt(language="de")

        entry = get_default_degraded_store().snapshot()[0]
        targets = {c.target for c in entry.action_chips}
        assert "/settings/voice" in targets

    def test_supported_languages_present_in_metadata(self) -> None:
        from sovyx.voice.factory import _validate
        from sovyx.voice.stt import MOONSHINE_SUPPORTED_LANGUAGES

        stt_patch, cfg_patch = _patch_moonshine_stubs()
        with stt_patch, cfg_patch:
            _validate._create_stt(language="pt")

        entry = get_default_degraded_store().snapshot()[0]
        assert set(entry.metadata["engine_supported_languages"]) == set(
            MOONSHINE_SUPPORTED_LANGUAGES,
        )
