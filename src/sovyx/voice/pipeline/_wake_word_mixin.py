"""Wake-word router delegation mixin (extracted from ``_orchestrator.py``).

Owns the orchestrator's public + private wake-word management surface:
the per-mind register / unregister hot-reload entry points (Phase 8 /
T8.15 + ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T2) and the
adaptive-cooldown false-fire feedback dispatch (Phase 7 / T7.8 +
v0.32.0 round-3 paranoid audit C1).

Pre-extraction this surface lived as 3 methods on the single-class
``VoicePipeline`` god file. See CLAUDE.md anti-pattern #16 (god files
> 500 LOC mixed responsibilities) for the carve-out rationale —
second strike of Phase 5.F.19+ (heartbeat extraction was the first).

Anti-pattern #32 contract (mixin method-via-MRO stub shadowing): this
mixin doesn't call any host-only methods, so no TYPE_CHECKING-block
forward declarations are needed for cross-mixin invocations. The
host-owned attribute reads (``_wake_word_router``, ``_wake_word``,
``_current_mind_id``) ARE forward-declared in TYPE_CHECKING so mypy
strict stays clean without creating runtime attributes that would
interfere with the host's own initialisation order.

The 3 wake-word methods stay accessible on ``VoicePipeline`` instances
through MRO (e.g. ``pipeline.register_mind_wake_word(...)``) so the
RPC handler in ``sovyx.engine._rpc_handlers``, the dashboard mind
wake-word toggle endpoint (``test_mind_wake_word_toggle_t3.py``),
and the CLI ``train-wake-word`` flow all keep working with zero
caller-side change.

State the mixin reads (initialised on the HOST in
``VoicePipeline.__init__``):

* ``_wake_word_router: WakeWordRouter | None`` — multi-mind router.
  ``None`` in single-mind mode; methods raise ``VoiceError`` with a
  remediation hint when the router is required.
* ``_wake_word: WakeWordDetector`` — single-mind fallback detector.
  Used only by ``_notify_wake_word_false_fire`` when the router is
  ``None``.
* ``_current_mind_id: MindId`` — per-turn authoritative active mind.
  Used by ``_notify_wake_word_false_fire`` to dispatch to the
  router-matched detector's adaptive-cooldown window.

External imports:

* ``MindId`` (``sovyx.engine.types``) — required at runtime for the
  ``MindId(...)`` cast in ``_notify_wake_word_false_fire``.
* ``VoiceError`` (``sovyx.engine.errors``) — lazy-imported inside
  ``register_mind_wake_word`` / ``unregister_mind_wake_word`` per
  the orchestrator's existing pattern.
* ``Path``, ``WakeWordConfig``, ``WakeWordDetector``, ``WakeWordRouter``
  — type-only, declared in TYPE_CHECKING.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.engine.types import MindId
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sovyx.voice._wake_word_router import WakeWordRouter
    from sovyx.voice.wake_word import WakeWordConfig, WakeWordDetector

logger = get_logger(__name__)


class WakeWordRouterMixin:
    """Per-mind wake-word router delegation + false-fire feedback dispatch.

    Mounted on :class:`sovyx.voice.pipeline._orchestrator.VoicePipeline`
    via multiple inheritance. The host owns the router + detector
    instance fields in ``__init__``; this mixin owns the public delegate
    methods + the false-fire feedback dispatcher.

    See module docstring for the full responsibility carve-out + the
    anti-pattern #32 cross-mixin reference contract.
    """

    if TYPE_CHECKING:
        # Host-owned attributes the mixin reads. Declared TYPE_CHECKING
        # so mypy strict resolves the references without creating
        # runtime attributes that would interfere with the host's own
        # initialisation order.
        _wake_word_router: WakeWordRouter | None
        _wake_word: WakeWordDetector
        _current_mind_id: str

    def register_mind_wake_word(
        self,
        mind_id: MindId,
        *,
        model_path: Path,
        config: WakeWordConfig | None = None,
    ) -> None:
        """Hot-reload a mind's wake-word detector with a new ONNX model.

        Phase 8 / T8.15 — wires the ``wake_word.register_mind`` RPC
        handler to the live :class:`~sovyx.voice._wake_word_router.WakeWordRouter`
        owned by this pipeline. Idempotent: re-registering the same
        ``mind_id`` replaces the prior detector (the prior ONNX session
        is garbage-collected normally; no manual close needed). Use case:
        the operator finishes ``sovyx voice train-wake-word ...`` and
        wants the new model active without restarting the daemon.

        Args:
            mind_id: Stable mind identifier (matches MindConfig.id).
            model_path: Filesystem path to the trained ``.onnx``
                checkpoint. The router does not validate the file;
                callers MUST verify the path exists + ends in ``.onnx``
                before invoking. The RPC handler in
                :mod:`sovyx.engine._rpc_handlers` performs that
                validation.
            config: Per-mind ``WakeWordConfig`` (cooldown, thresholds,
                etc.). Default ``None`` reuses the router's default.

        Raises:
            VoiceError: When the multi-mind ``WakeWordRouter`` is not
                configured (single-mind mode). Message includes a
                remediation hint.
            ValueError: Propagated from
                :meth:`WakeWordRouter.register_mind` when ``mind_id``
                is empty.
        """
        from sovyx.engine.errors import VoiceError  # noqa: PLC0415

        if self._wake_word_router is None:
            msg = (
                "wake-word router not configured (single-mind mode); "
                "hot-reload requires multi-mind setup. Restart the daemon "
                "to pick up the new model from "
                "``<data_dir>/wake_word_models/pretrained/``."
            )
            raise VoiceError(msg)
        self._wake_word_router.register_mind(
            mind_id,
            model_path=model_path,
            config=config,
        )

    def unregister_mind_wake_word(self, mind_id: MindId) -> bool:
        """Remove a mind's wake-word detector from the live router.

        Mission ``MISSION-wake-word-runtime-wireup-2026-05-03.md`` §T2
        — the symmetric inverse of :meth:`register_mind_wake_word`.
        Wires the ``wake_word.unregister_mind`` RPC handler to the
        live :class:`~sovyx.voice._wake_word_router.WakeWordRouter`
        owned by this pipeline.

        Use case: the operator flips
        :attr:`MindConfig.wake_word_enabled` from ``True`` to ``False``
        in the dashboard. T3's toggle endpoint persists the YAML +
        calls this method so the running pipeline drops the detector
        without a daemon restart. Idempotent on unknown mind_ids
        (the router itself is idempotent — see
        :meth:`WakeWordRouter.unregister_mind`).

        Args:
            mind_id: Stable mind identifier (matches MindConfig.id).

        Returns:
            ``True`` when the mind was previously registered and got
            removed; ``False`` when no detector existed for this id
            (idempotent no-op — caller can ignore or surface it).

        Raises:
            VoiceError: When the multi-mind ``WakeWordRouter`` is not
                configured (single-mind mode). Message includes a
                remediation hint mirroring
                :meth:`register_mind_wake_word`.
        """
        from sovyx.engine.errors import VoiceError  # noqa: PLC0415

        if self._wake_word_router is None:
            msg = (
                "wake-word router not configured (single-mind mode); "
                "unregister_mind requires multi-mind setup. The "
                "single-mind pipeline owns one detector via the legacy "
                "wake_word slot, not via the router."
            )
            raise VoiceError(msg)
        return self._wake_word_router.unregister_mind(mind_id)

    def _notify_wake_word_false_fire(self) -> None:
        """Forward a false-fire signal to the wake-word detector.

        Phase 7 / T7.8 — orchestrator → detector feedback for the
        adaptive-cooldown sliding window. The detector accumulates
        timestamps and elevates cooldown to ``cooldown_max_seconds``
        when the rolling-window count crosses the threshold.

        v0.32.0 / Round-3 paranoid audit C1 — multi-mind dispatch.
        When ``self._wake_word_router is not None`` (multi-mind mode)
        AND ``self._current_mind_id`` resolves to a router-matched
        detector, dispatch via :meth:`WakeWordRouter.note_false_fire`
        so the matched mind's adaptive cooldown window increments.
        Pre-fix (v0.31.x) the orchestrator always called
        ``self._wake_word.note_false_fire()`` — the single fallback
        detector that NEVER fired in multi-mind mode — which corrupted
        the per-mind sliding window: the matched mind's window stayed
        empty while an unrelated detector's window over-counted.

        Best-effort: a wake-word detector that doesn't expose
        ``note_false_fire`` (e.g. the factory's no-op stub when
        ``wake_word_enabled=False``) is silently skipped. The
        orchestrator's other false-fire paths (counter + log event)
        still fire regardless.
        """
        # Multi-mind path — dispatch to the matched mind's detector via
        # the router. ``_current_mind_id`` is always populated (defaults
        # to ``config.mind_id`` at construction; router match overrides
        # per turn at ``_handle_idle``); the router silently no-ops on
        # unknown mind_ids so a stale/cleared mind_id is safe.
        if self._wake_word_router is not None:
            try:
                self._wake_word_router.note_false_fire(MindId(self._current_mind_id))
            except Exception:  # noqa: BLE001 — observability path must not break the pipeline
                logger.exception("voice.wake_word.note_false_fire_failed")
            return

        # Single-mind path — fall through to the legacy detector.
        notify = getattr(self._wake_word, "note_false_fire", None)
        if notify is None:
            return
        try:
            notify()
        except Exception:  # noqa: BLE001 — observability path must not break the pipeline
            logger.exception("voice.wake_word.note_false_fire_failed")
