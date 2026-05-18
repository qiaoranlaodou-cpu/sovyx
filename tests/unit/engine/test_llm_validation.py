"""Unit tests — `sovyx.engine._llm_validation.validate_cloud_keys_at_boot` (Mission C6 §T2.6).

Coverage: opt-in gate, candidate discovery, per-key bounded timeout,
exception capture, empty-result invariants, env-snapshot helper.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sovyx.engine._llm_validation import (
    env_snapshot_for_validation,
    validate_cloud_keys_at_boot,
)
from sovyx.engine.config import LLMTuningConfig


def _make_config(*, enabled: bool, timeout_sec: float = 5.0) -> LLMTuningConfig:
    return LLMTuningConfig(
        boot_key_validation_enabled=enabled,
        boot_key_validation_timeout_sec=timeout_sec,
    )


class TestOptInGate:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty_dict(self) -> None:
        config = _make_config(enabled=False)
        result = await validate_cloud_keys_at_boot(
            env={"ANTHROPIC_API_KEY": "sk-test"},
            config=config,
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_enabled_with_no_candidates_returns_empty_dict(self) -> None:
        config = _make_config(enabled=True)
        result = await validate_cloud_keys_at_boot(env={}, config=config)
        assert result == {}


class TestProbeDispatch:
    @pytest.mark.asyncio
    async def test_valid_key_returns_true(self) -> None:
        config = _make_config(enabled=True)
        mock_provider = MagicMock()
        with (
            patch(
                "sovyx.engine._llm_validation.create_provider",
                return_value=mock_provider,
            ),
            patch(
                "sovyx.engine._llm_validation.test_provider",
                new=AsyncMock(return_value=(True, "OK")),
            ),
        ):
            result = await validate_cloud_keys_at_boot(
                env={"ANTHROPIC_API_KEY": "sk-ok"},
                config=config,
            )
        assert result == {"anthropic": True}

    @pytest.mark.asyncio
    async def test_invalid_key_returns_false(self) -> None:
        config = _make_config(enabled=True)
        mock_provider = MagicMock()
        with (
            patch(
                "sovyx.engine._llm_validation.create_provider",
                return_value=mock_provider,
            ),
            patch(
                "sovyx.engine._llm_validation.test_provider",
                new=AsyncMock(return_value=(False, "Auth failed: 401")),
            ),
        ):
            result = await validate_cloud_keys_at_boot(
                env={"ANTHROPIC_API_KEY": "sk-bad"},
                config=config,
            )
        assert result == {"anthropic": False}

    @pytest.mark.asyncio
    async def test_multiple_keys_concurrent(self) -> None:
        """Validates all configured keys in one bounded-timeout window."""
        config = _make_config(enabled=True)
        mock_provider = MagicMock()
        with (
            patch(
                "sovyx.engine._llm_validation.create_provider",
                return_value=mock_provider,
            ),
            patch(
                "sovyx.engine._llm_validation.test_provider",
                new=AsyncMock(return_value=(True, "OK")),
            ),
        ):
            result = await validate_cloud_keys_at_boot(
                env={
                    "ANTHROPIC_API_KEY": "sk-1",
                    "OPENAI_API_KEY": "sk-2",
                    "GOOGLE_API_KEY": "sk-3",
                },
                config=config,
            )
        assert result == {"anthropic": True, "openai": True, "google": True}


class TestExceptionCapture:
    @pytest.mark.asyncio
    async def test_create_provider_returns_none_is_invalid(self) -> None:
        config = _make_config(enabled=True)
        with patch(
            "sovyx.engine._llm_validation.create_provider",
            return_value=None,
        ):
            result = await validate_cloud_keys_at_boot(
                env={"ANTHROPIC_API_KEY": "sk-bad"},
                config=config,
            )
        assert result == {"anthropic": False}

    @pytest.mark.asyncio
    async def test_probe_timeout_is_invalid(self) -> None:
        # Pydantic bounds enforce ge=1.0; use the floor + a sleep that
        # exceeds it. Keeps the test bounded but compliant with the
        # tuning-config contract.
        config = _make_config(enabled=True, timeout_sec=1.0)
        mock_provider = MagicMock()

        async def _slow_probe(_provider: object) -> tuple[bool, str]:
            await asyncio.sleep(5.0)  # exceeds timeout
            return True, "OK"

        with (
            patch(
                "sovyx.engine._llm_validation.create_provider",
                return_value=mock_provider,
            ),
            patch(
                "sovyx.engine._llm_validation.test_provider",
                new=_slow_probe,
            ),
        ):
            result = await validate_cloud_keys_at_boot(
                env={"ANTHROPIC_API_KEY": "sk-slow"},
                config=config,
            )
        assert result == {"anthropic": False}

    @pytest.mark.asyncio
    async def test_probe_exception_is_invalid(self) -> None:
        config = _make_config(enabled=True)
        mock_provider = MagicMock()

        async def _raising_probe(_provider: object) -> tuple[bool, str]:
            msg = "connection refused"
            raise RuntimeError(msg)

        with (
            patch(
                "sovyx.engine._llm_validation.create_provider",
                return_value=mock_provider,
            ),
            patch(
                "sovyx.engine._llm_validation.test_provider",
                new=_raising_probe,
            ),
        ):
            result = await validate_cloud_keys_at_boot(
                env={"ANTHROPIC_API_KEY": "sk-flaky"},
                config=config,
            )
        assert result == {"anthropic": False}


class TestNeverRaises:
    @pytest.mark.asyncio
    async def test_function_never_raises_on_creator_exception(self) -> None:
        """Boot path MUST proceed regardless of validation failures."""
        config = _make_config(enabled=True)

        def _raising_creator(*_args: object, **_kwargs: object) -> None:
            msg = "import failed"
            raise ImportError(msg)

        with patch(
            "sovyx.engine._llm_validation.create_provider",
            side_effect=_raising_creator,
        ):
            # Must NOT raise — function captures every exception
            result = await validate_cloud_keys_at_boot(
                env={"ANTHROPIC_API_KEY": "sk-test"},
                config=config,
            )
        assert "anthropic" in result
        assert result["anthropic"] is False


class TestEnvSnapshot:
    def test_snapshot_is_defensive_copy(self) -> None:
        snapshot = env_snapshot_for_validation()
        assert isinstance(snapshot, dict)
        # Mutating the snapshot does not mutate os.environ
        snapshot["SOVYX_C6_TEST_VAR_QQQ"] = "test"
        import os

        assert "SOVYX_C6_TEST_VAR_QQQ" not in os.environ
