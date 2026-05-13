"""Tests for ``sovyx.voice.calibration._persist_device.persist_voice_input_device``.

Validates the Phase 2.T2.3 shared persistence helper that the dashboard
voice-enable endpoint AND the new ``sovyx voice setup`` CLI command
both consume to commit a mic choice to a per-mind ``mind.yaml``.

Contract pinned here:

* Non-empty device name → both fields written when host_api present;
  only ``voice_input_device_name`` written when host_api is None / "".
* Empty / whitespace-only device name → ``ValueError`` (caller must
  not invoke the helper for "no preference").
* Missing mind.yaml → ``FileNotFoundError`` with actionable message.
* In-memory mirror — best-effort: a frozen / restricted object does
  not break the disk-persist contract.
* Atomic + serial: relies on
  :class:`sovyx.engine.config_editor.ConfigEditor.set_scalar`'s
  temp-file + rename semantics; tested indirectly by reading back the
  file after the write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import yaml

from sovyx.voice.calibration._persist_device import persist_voice_input_device

if TYPE_CHECKING:
    from pathlib import Path


def _seed_mind_yaml(path: Path, *, name: str = "default") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"name: {name}\nid: {name}\n", encoding="utf-8")


class TestPersistVoiceInputDevice:
    @pytest.mark.asyncio()
    async def test_writes_both_fields_when_host_api_provided(self, tmp_path: Path) -> None:
        """Happy path — device + host_api both land in mind.yaml."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="Razer BlackShark V2 Pro",
            host_api="Windows WASAPI",
        )

        data = yaml.safe_load(mind_yaml.read_text(encoding="utf-8"))
        assert data["voice_input_device_name"] == "Razer BlackShark V2 Pro"
        assert data["voice_input_device_host_api"] == "Windows WASAPI"

    @pytest.mark.asyncio()
    async def test_writes_only_device_name_when_host_api_missing(self, tmp_path: Path) -> None:
        """host_api=None → device name persists; host_api field NOT written."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="Built-in Microphone",
            host_api=None,
        )

        data = yaml.safe_load(mind_yaml.read_text(encoding="utf-8"))
        assert data["voice_input_device_name"] == "Built-in Microphone"
        assert "voice_input_device_host_api" not in data

    @pytest.mark.asyncio()
    async def test_writes_only_device_name_when_host_api_empty_string(
        self, tmp_path: Path
    ) -> None:
        """host_api="" → treated the same as None (empty after strip)."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="Mic",
            host_api="   ",
        )

        data = yaml.safe_load(mind_yaml.read_text(encoding="utf-8"))
        assert data["voice_input_device_name"] == "Mic"
        assert "voice_input_device_host_api" not in data

    @pytest.mark.asyncio()
    async def test_strips_whitespace_from_values(self, tmp_path: Path) -> None:
        """Leading / trailing whitespace stripped before write."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="  Razer  ",
            host_api="  WASAPI  ",
        )

        data = yaml.safe_load(mind_yaml.read_text(encoding="utf-8"))
        assert data["voice_input_device_name"] == "Razer"
        assert data["voice_input_device_host_api"] == "WASAPI"

    @pytest.mark.asyncio()
    async def test_empty_device_name_raises_value_error(self, tmp_path: Path) -> None:
        """device_name="" → ValueError (caller must not invoke for "no preference")."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        with pytest.raises(ValueError, match="non-empty"):
            await persist_voice_input_device(
                mind_yaml_path=mind_yaml,
                device_name="",
            )

    @pytest.mark.asyncio()
    async def test_whitespace_only_device_name_raises_value_error(self, tmp_path: Path) -> None:
        """device_name='   ' → ValueError (post-strip empty)."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        with pytest.raises(ValueError, match="non-empty"):
            await persist_voice_input_device(
                mind_yaml_path=mind_yaml,
                device_name="   ",
            )

    @pytest.mark.asyncio()
    async def test_missing_mind_yaml_raises_file_not_found(self, tmp_path: Path) -> None:
        """No mind.yaml on disk → FileNotFoundError pointing at sovyx init."""
        missing = tmp_path / "ghost" / "mind.yaml"

        with pytest.raises(FileNotFoundError, match="sovyx init"):
            await persist_voice_input_device(
                mind_yaml_path=missing,
                device_name="Mic",
            )

    @pytest.mark.asyncio()
    async def test_in_memory_mirror_updated_when_provided(self, tmp_path: Path) -> None:
        """mind_config attributes set to match the persisted values."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        @dataclass
        class _FakeMindConfig:
            voice_input_device_name: str = ""
            voice_input_device_host_api: str = ""

        cfg = _FakeMindConfig()
        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="Razer",
            host_api="WASAPI",
            mind_config=cfg,
        )

        assert cfg.voice_input_device_name == "Razer"
        assert cfg.voice_input_device_host_api == "WASAPI"

    @pytest.mark.asyncio()
    async def test_in_memory_mirror_swallows_attribute_errors(self, tmp_path: Path) -> None:
        """A frozen / restricted mind_config does NOT break disk persist."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        class _FrozenLike:
            __slots__ = ()  # no attribute writes allowed

        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="Razer",
            host_api="WASAPI",
            mind_config=_FrozenLike(),
        )

        # Disk persist still succeeded.
        data = yaml.safe_load(mind_yaml.read_text(encoding="utf-8"))
        assert data["voice_input_device_name"] == "Razer"
        assert data["voice_input_device_host_api"] == "WASAPI"

    @pytest.mark.asyncio()
    async def test_preserves_existing_mind_yaml_fields(self, tmp_path: Path) -> None:
        """Persistence does NOT clobber unrelated mind.yaml content."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        mind_yaml.parent.mkdir(parents=True)
        mind_yaml.write_text(
            "name: default\nid: default\nlanguage: pt-BR\nvoice_id: af_heart\n",
            encoding="utf-8",
        )

        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="Razer",
            host_api="WASAPI",
        )

        data = yaml.safe_load(mind_yaml.read_text(encoding="utf-8"))
        # New fields added.
        assert data["voice_input_device_name"] == "Razer"
        assert data["voice_input_device_host_api"] == "WASAPI"
        # Existing fields preserved.
        assert data["name"] == "default"
        assert data["language"] == "pt-BR"
        assert data["voice_id"] == "af_heart"

    @pytest.mark.asyncio()
    async def test_overwrite_existing_values(self, tmp_path: Path) -> None:
        """Subsequent calls update the already-persisted values."""
        mind_yaml = tmp_path / "default" / "mind.yaml"
        _seed_mind_yaml(mind_yaml)

        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="OldMic",
            host_api="ALSA",
        )
        await persist_voice_input_device(
            mind_yaml_path=mind_yaml,
            device_name="NewMic",
            host_api="PipeWire",
        )

        data = yaml.safe_load(mind_yaml.read_text(encoding="utf-8"))
        assert data["voice_input_device_name"] == "NewMic"
        assert data["voice_input_device_host_api"] == "PipeWire"
