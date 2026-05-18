"""Unit tests — `sovyx llm setup` (Mission C6 §T3.1 setup wizard).

Companion to ``test_llm_doctor.py``. Covers the setup-wizard side that
``test_llm_doctor.py`` exercises partially:

* Interactive prompts (mocked via typer's CliRunner ``input``)
* Non-interactive happy paths (cloud + Ollama)
* Validation failures + exit-code contracts
* `secrets.env` persistence path resolution
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sovyx.cli.commands import llm as llm_cmd

runner = CliRunner()


class TestSetupInteractive:
    def test_interactive_ollama_happy_path(self) -> None:
        """Operator picks Ollama (option 10); reachable + models → exit 0."""
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.is_available = True
        mock_instance.ping = AsyncMock(return_value=True)
        mock_instance.list_models = AsyncMock(return_value=["llama3.1:latest"])
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            result = runner.invoke(
                llm_cmd.llm_app,
                ["setup"],
                input="10\n",  # Ollama is the 10th member
            )
        assert result.exit_code == 0
        assert "reachable" in result.output.lower()

    def test_interactive_invalid_choice_retries(self) -> None:
        """An out-of-range choice prompts retry until valid."""
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.is_available = True
        mock_instance.ping = AsyncMock(return_value=True)
        mock_instance.list_models = AsyncMock(return_value=["llama3.1:latest"])
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            result = runner.invoke(
                llm_cmd.llm_app,
                ["setup"],
                input="99\n10\n",  # invalid 99 → retry → 10
            )
        assert result.exit_code == 0
        assert "Invalid choice" in result.output


class TestNonInteractiveCloudHappyPath:
    def test_valid_anthropic_key_persists(self, tmp_path: Path) -> None:
        mock_provider = MagicMock()
        with (
            patch.object(llm_cmd, "create_provider", return_value=mock_provider),
            patch.object(
                llm_cmd,
                "test_provider",
                new=AsyncMock(return_value=(True, "OK")),
            ),
        ):
            result = runner.invoke(
                llm_cmd.llm_app,
                [
                    "setup",
                    "--non-interactive",
                    "--provider",
                    "anthropic",
                    "--api-key",
                    "sk-test-12345",
                    "--data-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0
        secrets_path = tmp_path / "secrets.env"
        assert secrets_path.exists()
        content = secrets_path.read_text(encoding="utf-8")
        assert "ANTHROPIC_API_KEY=sk-test-12345" in content


class TestNonInteractiveCloudSadPaths:
    def test_test_provider_returning_false_exits_one(self, tmp_path: Path) -> None:
        mock_provider = MagicMock()
        with (
            patch.object(llm_cmd, "create_provider", return_value=mock_provider),
            patch.object(
                llm_cmd,
                "test_provider",
                new=AsyncMock(return_value=(False, "Auth 401")),
            ),
        ):
            result = runner.invoke(
                llm_cmd.llm_app,
                [
                    "setup",
                    "--non-interactive",
                    "--provider",
                    "openai",
                    "--api-key",
                    "sk-bad",
                    "--data-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 1
        # No persistence on validation failure
        assert not (tmp_path / "secrets.env").exists()

    def test_create_provider_returning_none_exits_one(self, tmp_path: Path) -> None:
        with patch.object(llm_cmd, "create_provider", return_value=None):
            result = runner.invoke(
                llm_cmd.llm_app,
                [
                    "setup",
                    "--non-interactive",
                    "--provider",
                    "google",
                    "--api-key",
                    "sk-test",
                    "--data-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 1


class TestNonInteractiveOllamaSadPaths:
    def test_ollama_unreachable_exits_one(self) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.is_available = False
        mock_instance.ping = AsyncMock(return_value=False)
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            result = runner.invoke(
                llm_cmd.llm_app,
                ["setup", "--non-interactive", "--provider", "ollama"],
            )
        assert result.exit_code == 1

    def test_ollama_no_models_exits_one(self) -> None:
        from sovyx.llm.providers import ollama

        mock_instance = MagicMock()
        mock_instance.is_available = True
        mock_instance.ping = AsyncMock(return_value=True)
        mock_instance.list_models = AsyncMock(return_value=[])
        with patch.object(ollama, "OllamaProvider", return_value=mock_instance):
            result = runner.invoke(
                llm_cmd.llm_app,
                ["setup", "--non-interactive", "--provider", "ollama"],
            )
        assert result.exit_code == 1


class TestSecretsEnvPersistence:
    def test_persist_replaces_existing_key(self, tmp_path: Path) -> None:
        """Re-running setup with a new key REPLACES the old line, not appends."""
        secrets_path = tmp_path / "secrets.env"
        secrets_path.write_text("ANTHROPIC_API_KEY=old-key\nOTHER=keep\n", encoding="utf-8")

        mock_provider = MagicMock()
        with (
            patch.object(llm_cmd, "create_provider", return_value=mock_provider),
            patch.object(
                llm_cmd,
                "test_provider",
                new=AsyncMock(return_value=(True, "OK")),
            ),
        ):
            result = runner.invoke(
                llm_cmd.llm_app,
                [
                    "setup",
                    "--non-interactive",
                    "--provider",
                    "anthropic",
                    "--api-key",
                    "new-key",
                    "--data-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0
        content = secrets_path.read_text(encoding="utf-8")
        # Old key replaced, new key present, OTHER preserved.
        assert "ANTHROPIC_API_KEY=new-key" in content
        assert "ANTHROPIC_API_KEY=old-key" not in content
        assert "OTHER=keep" in content
