"""Unit tests for :mod:`sovyx.voice.health._linux_mixer_probe` log levels.

v0.38.0 / W3.F1 — F2-M09 (audit) closure. The
``/proc/asound/cards`` exists()-then-OSError TOCTOU race used to log
at DEBUG level, so operators investigating "no mixer detected" on a
system that should have mixer cards missed the signal entirely. This
file pins the WARNING-level promotion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

from sovyx.voice.health import _linux_mixer_probe as mod

if TYPE_CHECKING:
    import pytest


class _StubProcCards:
    """Minimal ``Path``-shaped stub: exists()=True + read_text raises OSError."""

    def exists(self) -> bool:
        return True

    def read_text(self, *_args: object, **_kwargs: object) -> str:
        msg = "transient FS race"
        raise OSError(msg)


class TestProcCardsReadFailureLogLevel:
    """When /proc/asound/cards exists() succeeds but read fails, log at WARNING."""

    def test_oserror_during_read_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Promoted from DEBUG → WARNING per audit F2-M09."""
        caplog.set_level(logging.DEBUG, logger="sovyx.voice.health._linux_mixer_probe")
        with (
            patch.object(mod, "sys") as sys_mock,
            patch("shutil.which", return_value="/usr/bin/amixer"),
            patch.object(mod, "_PROC_CARDS", _StubProcCards()),
        ):
            sys_mock.platform = "linux"
            result = mod.enumerate_alsa_mixer_snapshots()

        assert result == []
        # Find the structured log record + assert it was emitted at
        # WARNING (not DEBUG, the pre-fix level).
        warn_records = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "linux_mixer_proc_cards_read_failed" in r.getMessage()
        ]
        assert warn_records, (
            "expected a WARNING-level linux_mixer_proc_cards_read_failed event; "
            f"got: {[r.getMessage() for r in caplog.records]!r}"
        )
        # And conversely — there must NOT be a DEBUG-level record for
        # the same event (would mean the level got reverted).
        debug_records = [
            r
            for r in caplog.records
            if r.levelname == "DEBUG" and "linux_mixer_proc_cards_read_failed" in r.getMessage()
        ]
        assert not debug_records, (
            "linux_mixer_proc_cards_read_failed must NOT log at DEBUG (regression)"
        )
