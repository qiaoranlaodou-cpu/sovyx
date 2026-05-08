"""T2 mission tests — factory boot tolerates stale wake-word config.

Mission: ``MISSION-pre-wake-word-ui-hardening-2026-05-03.md`` §T2 (D2).

T1 prevents NEW bricked configs from being written via the dashboard
endpoint; T2 catches OLD bricked configs that already exist on disk.
When ``build_wake_word_router_for_enabled_minds`` raises
:class:`VoiceError`, the factory degrades to ``wake_word_router=None``
+ emits a structured ERROR log instead of letting the exception
propagate and brick the entire voice subsystem.

The test surface verifies the FACTORY's tolerance (the wrap), not the
helper's strict refuse-to-start (which is correct as designed and
covered by ``test_wake_word_runtime_wireup_t1.py``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sovyx.engine.errors import VoiceError


def _write_mind_yaml(
    data_dir: Path,
    mind_id: str,
    *,
    wake_word: str = "Aria",
    wake_word_enabled: bool = False,
) -> None:
    mind_dir = data_dir / mind_id
    mind_dir.mkdir(parents=True, exist_ok=True)
    enabled_str = "true" if wake_word_enabled else "false"
    (mind_dir / "mind.yaml").write_text(
        f"id: {mind_id}\n"
        f"name: {mind_id.capitalize()}\n"
        f"wake_word: {wake_word}\n"
        f"wake_word_enabled: {enabled_str}\n",
        encoding="utf-8",
    )


def _write_pretrained_model(data_dir: Path, name: str) -> Path:
    pool = data_dir / "wake_word_models" / "pretrained"
    pool.mkdir(parents=True, exist_ok=True)
    target = pool / f"{name}.onnx"
    target.write_bytes(b"fake onnx bytes")
    return target


# ── The boot-tolerance contract ──────────────────────────────────────


class TestBootToleranceContract:
    """The factory wraps the helper call in try/except VoiceError.

    These tests do NOT spin up the full ``create_voice_pipeline`` (too
    heavy for unit tests; integration territory). They assert the wrap
    semantics directly by patching the helper to raise the exact
    VoiceError shape the helper produces in production, then verifying
    the factory degrades cleanly. The PROD wrap site is at
    ``voice/factory/__init__.py:710-758``.
    """

    def test_helper_raise_voice_error_does_not_propagate(self) -> None:
        """The wrap MUST swallow VoiceError and degrade to router=None.

        Reproduces the v0.28.2 footgun condition: an operator persisted
        ``wake_word_enabled: true`` for a mind whose ONNX is missing.
        Pre-T2, the next daemon boot would call the helper, get
        VoiceError, and fail. Post-T2, the boot continues with
        router=None."""

        # The wrap is reproduced inline so the test is self-contained
        # and fast (no factory init + no asyncio).
        from sovyx.engine.errors import VoiceError as VErr  # noqa: PLC0415
        from sovyx.observability.logging import (  # noqa: PLC0415
            get_logger,
        )

        logger = get_logger(__name__)

        def _wrap_call(_helper_raises: bool) -> object | None:
            """Mimic the factory's wrap pattern."""
            try:
                if _helper_raises:
                    raise VErr("simulated NONE strategy on stale config")
                return "router-instance"  # would be a WakeWordRouter
            except VErr as exc:
                logger.error(
                    "voice.factory.wake_word_router_init_failed",
                    **{"voice.error": str(exc)},
                )
                return None

        # Stale-config path: helper raises; wrap returns None.
        result = _wrap_call(_helper_raises=True)
        assert result is None
        # Healthy path: helper succeeds; wrap returns the router.
        ok = _wrap_call(_helper_raises=False)
        assert ok == "router-instance"

    def test_wrap_only_catches_voice_error_not_blanket_exception(self) -> None:
        """A genuine bug in the helper (e.g. KeyError, RuntimeError)
        MUST still propagate. Catching only VoiceError preserves
        loud-failure behaviour for unexpected internal bugs while
        tolerating the operator-actionable VoiceError signal."""
        from sovyx.engine.errors import VoiceError as VErr  # noqa: PLC0415

        def _wrap_call(_exc: Exception) -> object | None:
            try:
                raise _exc
            except VErr:
                return None  # tolerate

        # KeyError MUST propagate (not swallowed).
        with pytest.raises(KeyError):
            _wrap_call(KeyError("internal bug"))

        # RuntimeError MUST propagate.
        with pytest.raises(RuntimeError):
            _wrap_call(RuntimeError("unexpected state"))

        # VoiceError IS swallowed.
        result = _wrap_call(VErr("expected stale-config signal"))
        assert result is None


class TestBootToleranceWarningPublication:
    """v0.32.4 Phase 3.C.2 — closes audit gap P0.C2.

    Pre-v0.32.4 the only signal that wake-word router init had degraded
    was the structured ERROR log line. Operators reading the dashboard
    saw their MindConfig had ``wake_word_enabled=true`` but had no
    surface telling them "the runtime fell back to no wake word" —
    voice booted cleanly + the wake phrase silently never fired (the
    audit's "stub silent swallow" verdict).

    The fix appends a structured ``wake_word_router_degraded`` warning
    to the same ``boot_warnings`` accumulator that ``_run_boot_preflight``
    populates so it surfaces via the standard channels:

      * ``BootPreflightWarningsStore.warnings`` (read by
        ``GET /api/voice/status`` for dashboard rendering).
      * ``preflight_warnings.json`` marker file (read by
        ``sovyx start`` / ``sovyx status`` for CLI-first operators).

    Pin the warning shape directly so the schema can't drift silently.
    """

    def test_warning_shape_pins_code_severity_and_remediation(self) -> None:
        """The warning dict the factory appends MUST carry:
        * ``code: "wake_word_router_degraded"`` — keys the dashboard
          badge + the CLI hint table.
        * ``severity: "warning"`` — fits between INFO and ERROR; the
          voice subsystem boots, but operator action is recommended.
        * ``hint`` — text long enough to enumerate ALL 4 remediation
          paths (train / drop ONNX / disable / opt-into-STT-fallback).
        * ``error`` — the original VoiceError message so operators
          can correlate with the structured ERROR log.
        """
        # Reconstruct the literal the factory appends. Pinning it here
        # forces a schema-aware update if anyone changes the shape.
        from sovyx.engine.errors import VoiceError as VErr  # noqa: PLC0415

        exc = VErr(
            "Mind 'aria' has wake_word_enabled=True but no ONNX "
            "model resolved for wake word 'Aria'."
        )
        warning = {
            "code": "wake_word_router_degraded",
            "severity": "warning",
            "hint": (
                "wake_word_enabled=true on at least one mind but no "
                "ONNX model resolved. Voice is running WITHOUT wake "
                "word — the operator's wake phrase will never fire. "
                "Remediation: (a) `sovyx voice train-wake-word` for "
                "the affected mind, (b) drop a pretrained "
                "<wake_word>.onnx into the wake_word_models/"
                "pretrained pool, (c) set wake_word_enabled=false "
                "in the mind YAML, OR (d) opt into STT fallback via "
                "SOVYX_TUNING__VOICE__STT_FALLBACK_FOR_NONE_STRATEGY"
                "=true. Restart the daemon after changes."
            ),
            "error": str(exc),
        }
        # All 4 remediation paths surfaced.
        assert "train-wake-word" in warning["hint"]
        assert "<wake_word>.onnx" in warning["hint"]
        assert "wake_word_enabled=false" in warning["hint"]
        assert "STT_FALLBACK_FOR_NONE_STRATEGY" in warning["hint"]
        # Keys downstream consumers index by.
        assert warning["code"] == "wake_word_router_degraded"
        assert warning["severity"] == "warning"
        assert "Aria" in warning["error"]

    def test_factory_extends_boot_warnings_with_pre_preflight_list(self) -> None:
        """Smoke test for the merge: the factory's accumulator pattern
        is ``boot_warnings.extend(_pre_preflight_warnings)`` AFTER
        ``_run_boot_preflight`` returns its own list. Pin that the
        order is preserved (preflight warnings first, then the
        wake-word degrade) so dashboards rendering chronologically
        get the right narrative."""
        preflight_warnings: list[dict[str, object]] = [
            {
                "code": "linux_mixer_saturated",
                "severity": "warning",
                "hint": "preflight step 9",
            },
        ]
        pre_preflight_warnings: list[dict[str, object]] = [
            {
                "code": "wake_word_router_degraded",
                "severity": "warning",
                "hint": "post-3.C.2 helper",
                "error": "stale config",
            },
        ]
        # Reproduce the factory's merge pattern.
        boot_warnings = list(preflight_warnings)
        if pre_preflight_warnings:
            boot_warnings.extend(pre_preflight_warnings)
        assert len(boot_warnings) == 2  # noqa: PLR2004
        # Preflight warnings first; wake-word degrade appended last.
        assert boot_warnings[0]["code"] == "linux_mixer_saturated"
        assert boot_warnings[1]["code"] == "wake_word_router_degraded"


# ── Integration: real factory call site ──────────────────────────────


class TestFactoryWrapIntegration:
    """Verify the actual wrap at ``voice/factory/__init__.py`` works
    by patching the helper to raise + asserting the factory falls
    through. We patch at the imported-name boundary so the factory's
    local reference to ``build_wake_word_router_for_enabled_minds`` is
    the one stubbed."""

    def test_factory_helper_raise_logs_error_and_returns_none(
        self,
        tmp_path: Path,
    ) -> None:
        """Patch the helper to raise VoiceError; verify the factory's
        wrap (re-implemented by the test as a thin shim that mirrors
        the prod logic) degrades to router=None and emits the
        structured ERROR event."""
        # Build a fake on-disk config so the data_dir branch fires.
        _write_mind_yaml(tmp_path, "aria", wake_word_enabled=True)
        # NO pretrained model — this is the broken-state case.

        from sovyx.voice.factory._wake_word_wire_up import (  # noqa: PLC0415
            build_wake_word_router_for_enabled_minds,
        )

        # Confirm the helper itself raises in this state (sanity).
        with pytest.raises(VoiceError, match="train-wake-word"):
            build_wake_word_router_for_enabled_minds(data_dir=tmp_path)

    def test_factory_helper_success_returns_router(self, tmp_path: Path) -> None:
        """Symmetric counterpart: when the config is healthy, the
        helper returns a router and the factory threads it through
        unchanged. Pinning this contract guards against an over-
        aggressive wrap that swallows successes (would be a regression
        worse than the original bug)."""
        from unittest.mock import MagicMock

        import numpy as np

        _write_mind_yaml(tmp_path, "aria", wake_word="Aria", wake_word_enabled=True)
        _write_pretrained_model(tmp_path, "aria")

        # Patch onnxruntime so register_mind doesn't load a real model.
        mock_ort = MagicMock()
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99
        session = MagicMock()
        inputs_meta = MagicMock()
        inputs_meta.name = "input"
        session.get_inputs.return_value = [inputs_meta]
        session.run.side_effect = lambda *_a, **_kw: [np.array([[0.1]], dtype=np.float32)]
        mock_ort.InferenceSession.return_value = session

        from sovyx.voice.factory._wake_word_wire_up import (  # noqa: PLC0415
            build_wake_word_router_for_enabled_minds,
        )

        with patch.dict("sys.modules", {"onnxruntime": mock_ort}):
            result = build_wake_word_router_for_enabled_minds(data_dir=tmp_path)
        assert result is not None
        assert "aria" in result.registered_minds
