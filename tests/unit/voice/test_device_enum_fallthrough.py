"""Tests for Phase 3.T3.2 — ``resolve_device`` OS-default fall-through WARN.

Validates the new ``voice.device_enum.os_default_fallthrough`` structured
log that fires when :func:`sovyx.voice.device_enum.resolve_device`
selects PortAudio's OS-default (or the first preferred entry, when no
``is_os_default`` flag is set on any candidate) because the caller
passed neither a ``requested_name`` nor a ``requested_index``.

Contract pinned here:

* Empty / missing request → WARN with kind + selected_device +
  selected_host_api + note pointing at ``sovyx voice setup``.
* Non-empty ``requested_name`` that matches → NO WARN (operator intent
  was honoured).
* Non-empty ``requested_name`` that does NOT match → falls through to
  preferred BUT no silent-sentinel WARN fires (the stale-name case is
  observability for the failover layer, not this sentinel).
* Integer ``requested_index`` that matches → NO WARN.
* ``preferred`` empty path → returns None, no WARN.

Companion to the T3.1 factory sentinel WARN (``voice.factory.input_device_unconfigured``).

CLAUDE.md "structlog routing makes capture_logs flaky" escape hatch:
this suite originally used :func:`structlog.testing.capture_logs` for
assertions. On Windows CI the project's structlog config routes
WARNING-level events through stdlib's ``logging`` module, where
pytest's ``caplog`` fixture intercepts them BEFORE the structlog
processor chain runs — ``capture_logs`` then returns an empty list
even though the WARN actually fired (the Windows CI failure on
v0.39.1 confirmed this). The documented escape hatch is to patch the
module-level ``logger`` object with a :class:`MagicMock` spy and
inspect ``spy.warning.call_args_list`` — works regardless of the
structlog routing config. Precedent: ``tests/unit/cli/test_generate_signing_key.py``
does this for ``voice.calibration.signing_key.generated``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sovyx.voice import device_enum
from sovyx.voice.device_enum import DeviceEntry, resolve_device

_FALLTHROUGH_EVENT = "voice.device_enum.os_default_fallthrough"


def _entry(
    *,
    index: int,
    name: str,
    host_api_name: str = "Windows WASAPI",
    is_os_default: bool = False,
    max_input_channels: int = 1,
    max_output_channels: int = 0,
) -> DeviceEntry:
    return DeviceEntry(
        index=index,
        name=name,
        canonical_name=name.strip().lower(),
        host_api_index=0,
        host_api_name=host_api_name,
        max_input_channels=max_input_channels,
        max_output_channels=max_output_channels,
        default_samplerate=48_000,
        is_os_default=is_os_default,
    )


def _fallthrough_warn_calls(spy: MagicMock) -> list:
    """Filter the spy's ``warning(...)`` calls to the fall-through event."""
    return [
        call
        for call in spy.warning.call_args_list
        if call.args and call.args[0] == _FALLTHROUGH_EVENT
    ]


class TestOsDefaultFallthroughWarn:
    """Phase 3.T3.2 — WARN fires on silent OS-default selection."""

    def test_warn_when_no_request_with_os_default_present(self, monkeypatch: object) -> None:
        """``requested_name=None, requested_index=None`` → OS-default
        selected + structured WARN with selected device + host API."""
        razer = _entry(index=2, name="Razer", is_os_default=True)
        builtin = _entry(index=1, name="Built-in")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [builtin, razer]
        )

        spy = MagicMock()
        with patch.object(device_enum, "logger", spy):
            resolved = resolve_device(
                requested_index=None,
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        assert resolved is razer
        warns = _fallthrough_warn_calls(spy)
        assert len(warns) == 1
        kwargs = warns[0].kwargs
        assert kwargs["kind"] == "input"
        assert kwargs["selected_device"] == "Razer"
        assert kwargs["selected_host_api"] == "Windows WASAPI"
        assert "sovyx voice setup" in kwargs["note"]

    def test_warn_when_no_request_and_no_os_default_flag(self, monkeypatch: object) -> None:
        """No is_os_default on any candidate → first preferred entry
        wins + WARN still fires with the alternate note."""
        a = _entry(index=0, name="A")
        b = _entry(index=1, name="B")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [a, b]
        )

        spy = MagicMock()
        with patch.object(device_enum, "logger", spy):
            resolved = resolve_device(
                requested_index=None,
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        assert resolved is not None
        warns = _fallthrough_warn_calls(spy)
        assert len(warns) == 1
        assert "no OS-default flag" in warns[0].kwargs["note"]

    def test_no_warn_when_requested_name_honoured(self, monkeypatch: object) -> None:
        """A successful name match means we never reached the fall-through
        branch — no WARN expected."""
        razer = _entry(index=2, name="Razer", is_os_default=True)
        builtin = _entry(index=1, name="Built-in")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [razer, builtin]
        )

        spy = MagicMock()
        with patch.object(device_enum, "logger", spy):
            resolved = resolve_device(
                requested_index=None,
                requested_name="Razer",
                requested_host_api=None,
                kind="input",
            )
        assert resolved is razer
        assert _fallthrough_warn_calls(spy) == []

    def test_no_warn_when_requested_index_honoured(self, monkeypatch: object) -> None:
        """A successful index match means we never reached the fall-through
        branch — no WARN expected. (``requested_index`` is a LIST index into
        the ``entries`` enumeration, not a lookup-by-DeviceEntry.index.)"""
        razer = _entry(index=2, name="Razer", is_os_default=True)
        builtin = _entry(index=1, name="Built-in")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [builtin, razer]
        )

        spy = MagicMock()
        with patch.object(device_enum, "logger", spy):
            resolved = resolve_device(
                requested_index=0,  # entries[0] == builtin
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        assert resolved is builtin
        assert _fallthrough_warn_calls(spy) == []

    def test_warn_when_requested_name_does_not_match(self, monkeypatch: object) -> None:
        """A stale ``requested_name`` (e.g. unplugged USB) falls through
        to OS-default. The silent-fallthrough WARN does NOT fire here —
        a non-empty ``requested_name`` carried real operator intent;
        intent-mismatch is observability owned by the failover layer,
        not by this sentinel."""
        razer = _entry(index=2, name="Razer", is_os_default=True)
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [razer]
        )

        spy = MagicMock()
        with patch.object(device_enum, "logger", spy):
            resolved = resolve_device(
                requested_index=None,
                requested_name="UNPLUGGED_GHOST_MIC",
                requested_host_api=None,
                kind="input",
            )
        assert resolved is razer
        assert _fallthrough_warn_calls(spy) == [], (
            "stale-name fall-through is NOT a sentinel slip — should "
            "not fire the empty-request WARN"
        )

    def test_returns_none_without_warn_when_no_devices(self, monkeypatch: object) -> None:
        """Empty enumeration → returns None, no WARN (caller handles)."""
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: []
        )

        spy = MagicMock()
        with patch.object(device_enum, "logger", spy):
            resolved = resolve_device(
                requested_index=None,
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        assert resolved is None
        assert _fallthrough_warn_calls(spy) == []
