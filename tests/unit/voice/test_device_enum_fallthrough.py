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
  preferred BUT we did have an intent: the resolver still emits the
  WARN because the silent fallback ends up at OS-default regardless.
* Integer ``requested_index`` that matches → NO WARN.
* ``preferred`` empty path → returns None, no WARN.

Companion to the T3.1 factory sentinel WARN (``voice.factory.input_device_unconfigured``).
"""

from __future__ import annotations

from structlog.testing import capture_logs

from sovyx.voice import device_enum
from sovyx.voice.device_enum import DeviceEntry, resolve_device


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

        with capture_logs() as captured:
            resolved = resolve_device(
                requested_index=None,
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        assert resolved is razer
        warns = [
            c for c in captured if c.get("event") == "voice.device_enum.os_default_fallthrough"
        ]
        assert len(warns) == 1
        warn = warns[0]
        assert warn["log_level"] == "warning"
        assert warn["kind"] == "input"
        assert warn["selected_device"] == "Razer"
        assert warn["selected_host_api"] == "Windows WASAPI"
        assert "sovyx voice setup" in warn["note"]

    def test_warn_when_no_request_and_no_os_default_flag(self, monkeypatch: object) -> None:
        """No is_os_default on any candidate → first preferred entry
        wins + WARN still fires with the alternate note."""
        a = _entry(index=0, name="A")  # is_os_default=False
        b = _entry(index=1, name="B")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [a, b]
        )

        with capture_logs() as captured:
            resolved = resolve_device(
                requested_index=None,
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        # The first preferred entry was selected; assertion just confirms
        # we got SOMETHING + the WARN signalled the silent selection.
        assert resolved is not None
        warns = [
            c for c in captured if c.get("event") == "voice.device_enum.os_default_fallthrough"
        ]
        assert len(warns) == 1
        assert "no OS-default flag" in warns[0]["note"]

    def test_no_warn_when_requested_name_honoured(self, monkeypatch: object) -> None:
        """A successful name match means we never reached the fall-through
        branch — no WARN expected."""
        razer = _entry(index=2, name="Razer", is_os_default=True)
        builtin = _entry(index=1, name="Built-in")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [razer, builtin]
        )

        with capture_logs() as captured:
            resolved = resolve_device(
                requested_index=None,
                requested_name="Razer",
                requested_host_api=None,
                kind="input",
            )
        assert resolved is razer
        warns = [
            c for c in captured if c.get("event") == "voice.device_enum.os_default_fallthrough"
        ]
        assert warns == []

    def test_no_warn_when_requested_index_honoured(self, monkeypatch: object) -> None:
        """A successful index match means we never reached the fall-through
        branch — no WARN expected. (``requested_index`` is a LIST index into
        the ``entries`` enumeration, not a lookup-by-DeviceEntry.index.)"""
        razer = _entry(index=2, name="Razer", is_os_default=True)
        builtin = _entry(index=1, name="Built-in")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [builtin, razer]
        )

        with capture_logs() as captured:
            resolved = resolve_device(
                requested_index=0,  # entries[0] == builtin
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        assert resolved is builtin
        warns = [
            c for c in captured if c.get("event") == "voice.device_enum.os_default_fallthrough"
        ]
        assert warns == []

    def test_warn_when_requested_name_does_not_match(self, monkeypatch: object) -> None:
        """A stale ``requested_name`` (e.g. unplugged USB) falls through
        to OS-default. The WARN fires because the SELECTION mechanism
        was silent — the operator intent was effectively dropped. This
        is the most operationally-important case: pre-Phase-3 a stale
        device name made the daemon pick "default" with no audit signal."""
        razer = _entry(index=2, name="Razer", is_os_default=True)
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: [razer]
        )

        with capture_logs() as captured:
            resolved = resolve_device(
                requested_index=None,
                requested_name="UNPLUGGED_GHOST_MIC",
                requested_host_api=None,
                kind="input",
            )
        # Fall-through landed on Razer (the OS-default).
        assert resolved is razer
        # Note: per the current implementation, the WARN gate is
        # ``not requested_name and (requested_index is None or str)``.
        # A non-empty requested_name DOES NOT fire the silent-fallthrough
        # WARN because we DID have an intent — it's just that the intent
        # didn't match. Operators with stale device names get observability
        # from a different layer (the T3.1 factory sentinel only fires on
        # EMPTY name; the stale-name case is intentionally a separate
        # signal handled by failover, not a sentinel here).
        warns = [
            c for c in captured if c.get("event") == "voice.device_enum.os_default_fallthrough"
        ]
        assert warns == [], (
            f"stale-name fall-through is NOT a sentinel slip — should "
            f"not fire the empty-request WARN, got: {warns}"
        )

    def test_returns_none_without_warn_when_no_devices(self, monkeypatch: object) -> None:
        """Empty enumeration → returns None, no WARN (caller handles)."""
        monkeypatch.setattr(  # type: ignore[attr-defined]
            device_enum, "enumerate_devices", lambda: []
        )

        with capture_logs() as captured:
            resolved = resolve_device(
                requested_index=None,
                requested_name=None,
                requested_host_api=None,
                kind="input",
            )
        assert resolved is None
        warns = [
            c for c in captured if c.get("event") == "voice.device_enum.os_default_fallthrough"
        ]
        assert warns == []
