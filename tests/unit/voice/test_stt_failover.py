"""W2.1 / G-P1-4 — FailoverSTTEngine: recover, don't just telemeter.

Pins the failover mechanism with fake primary/secondary engines: failover on
a primary raise OR an S2 timeout-delta (never on genuine silence), the
circuit breaker that skips a down primary, the half-open recovery probe, and
the honest fallthrough when no secondary is available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from sovyx.engine.errors import VoiceError
from sovyx.voice.stt import STTEngine, TranscriptionResult
from sovyx.voice.stt_failover import FailoverSTTEngine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_AUDIO = np.zeros(160, dtype=np.float32)


class _FakeSTT(STTEngine):
    """Controllable fake STT engine.

    ``behaviour`` is a callable invoked per transcribe to decide the outcome:
    it may return a str (→ TranscriptionResult.text), raise, or bump the
    engine's ``timeout_count`` to simulate an S2 timeout.
    """

    def __init__(
        self,
        *,
        text: str = "primary",
        raise_exc: Exception | None = None,
        bump_timeout: bool = False,
        init_raises: Exception | None = None,
    ) -> None:
        self._text = text
        self._raise_exc = raise_exc
        self._bump_timeout = bump_timeout
        self._init_raises = init_raises
        self.timeout_count = 0
        self.transcribe_calls = 0
        self.init_calls = 0
        self.closed = False

    async def initialize(self) -> None:
        self.init_calls += 1
        if self._init_raises is not None:
            raise self._init_raises

    async def transcribe(
        self, audio: np.ndarray, sample_rate: int = 16_000
    ) -> TranscriptionResult:
        del audio, sample_rate
        self.transcribe_calls += 1
        if self._bump_timeout:
            self.timeout_count += 1
        if self._raise_exc is not None:
            raise self._raise_exc
        return TranscriptionResult(text=self._text)

    def transcribe_streaming(
        self, audio_stream: AsyncIterator[tuple[np.ndarray, int]]
    ) -> AsyncIterator[object]:  # pragma: no cover — delegated, not exercised
        del audio_stream
        raise NotImplementedError

    async def close(self) -> None:
        self.closed = True


class TestFailoverHappyPath:
    @pytest.mark.asyncio
    async def test_primary_success_does_not_touch_secondary(self) -> None:
        primary = _FakeSTT(text="hello")
        secondary = _FakeSTT(text="cloud")
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()

        result = await engine.transcribe(_AUDIO)

        assert result.text == "hello"
        assert secondary.transcribe_calls == 0

    @pytest.mark.asyncio
    async def test_empty_result_without_timeout_is_genuine_silence(self) -> None:
        # Primary returns empty text but did NOT time out → real silence,
        # NOT a failure. Must NOT fail over (no cloud spam on quiet moments).
        primary = _FakeSTT(text="")
        secondary = _FakeSTT(text="cloud")
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()

        result = await engine.transcribe(_AUDIO)

        assert result.text == ""
        assert secondary.transcribe_calls == 0


class TestFailoverTriggers:
    @pytest.mark.asyncio
    async def test_primary_raise_fails_over_to_secondary(self) -> None:
        primary = _FakeSTT(raise_exc=RuntimeError("onnx down"))
        secondary = _FakeSTT(text="cloud recovery")
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()

        result = await engine.transcribe(_AUDIO)

        assert result.text == "cloud recovery"
        assert secondary.transcribe_calls == 1

    @pytest.mark.asyncio
    async def test_primary_timeout_delta_fails_over(self) -> None:
        # Primary returns (empty) BUT bumps timeout_count → S2 timeout signal.
        primary = _FakeSTT(text="", bump_timeout=True)
        secondary = _FakeSTT(text="cloud recovery")
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()

        result = await engine.transcribe(_AUDIO)

        assert result.text == "cloud recovery"
        assert secondary.transcribe_calls == 1


class TestNoSecondary:
    @pytest.mark.asyncio
    async def test_no_secondary_reraises_instead_of_masking(self) -> None:
        primary = _FakeSTT(raise_exc=RuntimeError("down"))
        secondary = _FakeSTT(init_raises=ImportError("no openai"))
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()  # secondary init fails → failover disabled

        with pytest.raises(VoiceError):
            await engine.transcribe(_AUDIO)

    @pytest.mark.asyncio
    async def test_secondary_also_fails_raises(self) -> None:
        primary = _FakeSTT(raise_exc=RuntimeError("down"))
        secondary = _FakeSTT(raise_exc=RuntimeError("cloud down too"))
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()

        with pytest.raises(VoiceError):
            await engine.transcribe(_AUDIO)


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_breaker_opens_and_skips_primary(self) -> None:
        primary = _FakeSTT(raise_exc=RuntimeError("down"))
        secondary = _FakeSTT(text="cloud")
        engine = FailoverSTTEngine(primary, secondary, breaker_threshold=2)
        await engine.initialize()

        # Two failovers trip the breaker.
        await engine.transcribe(_AUDIO)
        await engine.transcribe(_AUDIO)
        calls_after_trip = primary.transcribe_calls

        # Next call: breaker open → primary is SKIPPED, secondary used directly.
        result = await engine.transcribe(_AUDIO)
        assert result.text == "cloud"
        assert primary.transcribe_calls == calls_after_trip  # primary not called

    @pytest.mark.asyncio
    async def test_breaker_half_open_probe_recovers_primary(self) -> None:
        primary = _FakeSTT(raise_exc=RuntimeError("down"))
        secondary = _FakeSTT(text="cloud")
        engine = FailoverSTTEngine(
            primary, secondary, breaker_threshold=1, breaker_probe_interval=2
        )
        await engine.initialize()

        await engine.transcribe(_AUDIO)  # trips breaker (threshold=1)
        # Primary recovers.
        primary._raise_exc = None  # noqa: SLF001
        primary._text = "primary back"  # noqa: SLF001

        # First open call uses the secondary directly (within probe interval)...
        first = await engine.transcribe(_AUDIO)  # calls_since_probe=1 < 2 → secondary
        assert first.text == "cloud"
        # ...the 2nd open call is the half-open probe → retries the primary.
        result = await engine.transcribe(_AUDIO)  # probe → primary success → resets
        assert result.text == "primary back"

        # Breaker reset: a subsequent failure-free call still hits primary.
        result2 = await engine.transcribe(_AUDIO)
        assert result2.text == "primary back"


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_initialize_initializes_both(self) -> None:
        primary = _FakeSTT()
        secondary = _FakeSTT()
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()
        assert primary.init_calls == 1
        assert secondary.init_calls == 1

    @pytest.mark.asyncio
    async def test_close_closes_both(self) -> None:
        primary = _FakeSTT()
        secondary = _FakeSTT()
        engine = FailoverSTTEngine(primary, secondary)
        await engine.initialize()
        await engine.close()
        assert primary.closed
        assert secondary.closed

    def test_state_proxies_primary(self) -> None:
        # The factory's post-initialize READY guard reads stt.state; the
        # wrapper must proxy the primary's so the guard stays meaningful.
        primary = _FakeSTT()
        primary.state = "ready_sentinel"  # type: ignore[attr-defined]
        engine = FailoverSTTEngine(primary, _FakeSTT())
        assert engine.state == "ready_sentinel"

    def test_state_none_when_primary_has_no_state(self) -> None:
        engine = FailoverSTTEngine(_FakeSTT(), _FakeSTT())
        assert engine.state is None
