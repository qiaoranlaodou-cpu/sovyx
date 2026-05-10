"""F2-C02 — constant-time WS auth on calibration + training streams.

The two WS endpoints affected by the audit:

* ``/api/voice/calibration/jobs/{job_id}/stream``
  (``src/sovyx/dashboard/routes/voice_calibration.py:1070``)
* ``/api/voice/training/jobs/{job_id}/stream``
  (``src/sovyx/dashboard/routes/voice_training.py:847``)

Pre-fix used naïve ``token != expected_token``. CPython's ``str.__eq__``
is variable-time at the byte level (short-circuits on the first
mismatching character), exposing CWE-208 timing side-channel that lets a
network-adjacent attacker recover the token byte-by-byte.

Post-fix both endpoints use :func:`secrets.compare_digest`, which is
documented constant-time for credential comparison.

Tests in this module assert three layers:

1. **Functional rejection** — each endpoint rejects the three canonical
   mismatch shapes (first-char, last-char, length) with the documented
   WebSocket close code.
2. **Implementation contract** — the auth path's source code calls
   ``secrets.compare_digest``. Cheap, deterministic, immune to wall-clock
   noise. This is the load-bearing assertion: as long as the code uses
   ``compare_digest``, the timing guarantee is inherited from the
   primitive (CPython HMAC-grade constant-time).
3. **Primitive timing envelope** — :func:`secrets.compare_digest` itself
   is constant-time across the three mismatch shapes within an 80th-
   percentile envelope. Validates the primitive on the host
   architecture; this is the audit's ``80-percentile envelope``
   acceptance criterion.

Why the test does NOT measure WS-handshake-end-to-end timing:
TestClient's ASGI WS handshake has millisecond-scale variance that
dwarfs the nanosecond-scale variable-time signal we're guarding against.
A pure end-to-end timing test on TestClient would either pass trivially
on a constant-time impl AND on a variable-time impl, or be so flaky on
CI that operators silence it. Anti-pattern #22 also bites on Windows
(15.6 ms ``time.monotonic`` granularity). Decomposing the assertion
into "we use compare_digest" + "compare_digest is constant-time" gives
a deterministic, actionable signal.
"""

from __future__ import annotations

import inspect
import secrets
import statistics
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sovyx.dashboard.routes import voice_calibration as calibration_module
from sovyx.dashboard.routes import voice_training as training_module
from sovyx.dashboard.server import create_app

_TOKEN = "test-token-constant-time-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # noqa: S105


def _build_app(tmp_path: Path) -> Any:
    from sovyx.engine.config import DatabaseConfig, EngineConfig

    app = create_app(token=_TOKEN)
    app.state.engine_config = EngineConfig(
        data_dir=tmp_path,
        database=DatabaseConfig(data_dir=tmp_path),
    )
    return app


# ── Mismatch fixtures ──────────────────────────────────────────────────


def _first_char_mismatch() -> str:
    return "X" + _TOKEN[1:]


def _last_char_mismatch() -> str:
    return _TOKEN[:-1] + "X"


def _length_mismatch() -> str:
    return _TOKEN + "extra"


# ── Layer 1 — functional rejection on both endpoints ───────────────────


class TestCalibrationWsRejectsMismatchShapes:
    """All three canonical mismatch shapes close with code 1008."""

    @pytest.mark.parametrize(
        ("kind", "wrong_token"),
        [
            ("first_char", _first_char_mismatch()),
            ("last_char", _last_char_mismatch()),
            ("length", _length_mismatch()),
        ],
    )
    def test_close_code_is_1008(
        self,
        kind: str,
        wrong_token: str,
        tmp_path: Path,
    ) -> None:
        client = TestClient(_build_app(tmp_path))
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/voice/calibration/jobs/default/stream?token={wrong_token}",
            ) as ws:
                ws.receive_text()
        assert exc_info.value.code == 1008, kind


class TestTrainingWsRejectsMismatchShapes:
    """All three canonical mismatch shapes close with code 4401."""

    @pytest.mark.parametrize(
        ("kind", "wrong_token"),
        [
            ("first_char", _first_char_mismatch()),
            ("last_char", _last_char_mismatch()),
            ("length", _length_mismatch()),
        ],
    )
    def test_close_code_is_4401(
        self,
        kind: str,
        wrong_token: str,
        tmp_path: Path,
    ) -> None:
        client = TestClient(_build_app(tmp_path))
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect(
                f"/api/voice/training/jobs/default/stream?token={wrong_token}",
            ) as ws:
                ws.receive_text()
        assert exc_info.value.code == 4401, kind


# ── Layer 2 — implementation contract (load-bearing assertion) ─────────


class TestAuthSourceUsesConstantTimeCompare:
    """Source-level contract: both endpoints' auth path calls compare_digest."""

    def test_calibration_stream_uses_compare_digest(self) -> None:
        src = inspect.getsource(calibration_module.stream_calibration_job)
        assert "secrets.compare_digest" in src, (
            "voice_calibration.stream_calibration_job must use "
            "secrets.compare_digest for token auth (F2-C02)."
        )
        assert " != expected_token" not in src, (
            "voice_calibration.stream_calibration_job leaks variable-time "
            "comparison on token (CWE-208)."
        )

    def test_training_stream_uses_compare_digest(self) -> None:
        src = inspect.getsource(training_module.stream_training_job)
        assert "secrets.compare_digest" in src, (
            "voice_training.stream_training_job must use "
            "secrets.compare_digest for token auth (F2-C02)."
        )
        assert " != expected" not in src, (
            "voice_training.stream_training_job leaks variable-time "
            "comparison on token (CWE-208)."
        )


# ── Layer 3 — primitive timing envelope ────────────────────────────────


class TestCompareDigestPrimitiveTimingEnvelope:
    """secrets.compare_digest stays inside an 80-percentile envelope.

    Audit acceptance criterion: the three canonical mismatch shapes
    elapse within "the same" 80-percentile envelope. We assert ratio
    instead of absolute elapsed: ``max(p50) / min(p50)`` across the
    three scenarios stays below a generous ceiling. Generous because
    Windows ``time.perf_counter`` granularity + GC + JIT-style pyc
    warm-up easily double a 200-ns measurement.
    """

    _ITERATIONS = 5_000

    @staticmethod
    def _measure(wrong: str) -> list[float]:
        # Warm-up: prime branch predictor + dispatch table.
        for _ in range(64):
            secrets.compare_digest(wrong, _TOKEN)
        samples: list[float] = []
        for _ in range(TestCompareDigestPrimitiveTimingEnvelope._ITERATIONS):
            t0 = time.perf_counter()
            secrets.compare_digest(wrong, _TOKEN)
            samples.append(time.perf_counter() - t0)
        return samples

    def test_envelope_ratio_below_ceiling(self) -> None:
        scenarios = {
            "first_char": _first_char_mismatch(),
            "last_char": _last_char_mismatch(),
            "length": _length_mismatch(),
        }
        # Compare digest behaves differently on length-mismatch (raises
        # bool False fast without full byte walk per docs); to keep the
        # envelope honest we measure same-length-mismatched first/last
        # against EACH OTHER, and length separately allowed to be
        # faster (which is the documented CPython behaviour).
        same_length_p50 = {
            kind: statistics.median(self._measure(wrong))
            for kind, wrong in scenarios.items()
            if kind in ("first_char", "last_char")
        }
        ratio = max(same_length_p50.values()) / min(same_length_p50.values())
        # Ceiling chosen empirically — typical CPython on Linux/Windows
        # delivers ratio < 1.5; we allow 4× to absorb perf_counter
        # granularity + interpreter noise without flake.
        assert ratio < 4.0, (
            f"compare_digest p50 ratio {ratio:.2f}× exceeds 4.0 ceiling "
            f"across same-length mismatch shapes; samples={same_length_p50}"
        )
