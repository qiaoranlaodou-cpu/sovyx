"""Tests for resolve_active_mic_card (v0.31.5 LE-1).

The helper bridges v0.31.4 GAP 5: it maps the operator's persisted
``MindConfig.voice_input_device_name`` to an ALSA card index by
parsing ``arecord -l`` output. ``None`` is the safe fallback that
preserves pre-v0.31.4 behaviour at every caller.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from sovyx.voice.calibration import _active_mic
from sovyx.voice.calibration._active_mic import resolve_active_mic_card

_ARECORD_L_OUTPUT = """**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC256 Analog [ALC256 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: Pro [Razer BlackShark V2 Pro], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["arecord", "-l"], returncode=returncode, stdout=stdout, stderr=""
    )


class TestResolveActiveMicCard:
    """resolve_active_mic_card returns the matching ALSA card index."""

    def test_substring_match_returns_card_index(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer BlackShark V2 Pro")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_case_insensitive_partial_match(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 2

    def test_first_card_match(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="HDA Intel PCH")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) == 0

    def test_none_mind_config_returns_none(self) -> None:
        assert resolve_active_mic_card(mind_config=None) is None

    def test_empty_persisted_name_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="")
        assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_whitespace_persisted_name_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="   ")
        assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_unavailable_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with patch.object(_active_mic.shutil, "which", return_value=None):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_oserror_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(_active_mic.subprocess, "run", side_effect=OSError("boom")),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_timeout_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(cmd="arecord", timeout=5),
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_arecord_nonzero_exit_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Razer")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(_active_mic.subprocess, "run", return_value=_completed("", returncode=1)),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_no_match_returns_none(self) -> None:
        mind_config = SimpleNamespace(voice_input_device_name="Bose QuietComfort")
        with (
            patch.object(_active_mic.shutil, "which", return_value="/usr/bin/arecord"),
            patch.object(
                _active_mic.subprocess, "run", return_value=_completed(_ARECORD_L_OUTPUT)
            ),
        ):
            assert resolve_active_mic_card(mind_config=mind_config) is None

    def test_missing_attr_returns_none(self) -> None:
        mind_config = SimpleNamespace()
        assert resolve_active_mic_card(mind_config=mind_config) is None
