"""F2-H03 — `_tail_prompts_file` must not block the event loop on slow I/O.

Pre-fix `_wizard_orchestrator.py:397-398` ran ``Path.exists`` +
``Path.read_text`` synchronously inside an ``async def`` polled every
``_PROMPTS_POLL_INTERVAL_S`` (=0.5s). On a slow filesystem (NFS,
network share, encrypted volume, container overlay under load) the
event loop blocked for the full read latency every iteration —
calibration UI froze, all sibling coroutines starved (anti-pattern #14).

Post-fix the read happens via ``asyncio.to_thread`` so the loop yields
during the blocking syscall. The exists()-then-read race is collapsed
by catching ``FileNotFoundError`` inside the worker (the worker returns
``None`` instead of raising the OS error, the async branch then sleeps
the poll interval and continues).

Test strategy: monkey-patch ``Path.read_text`` to artificially block for
200 ms, then assert that a sentinel coroutine running concurrently makes
forward progress while the orchestrator's tail loop is in the middle of
its read. If the read held the event loop, the sentinel could not
advance.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from sovyx.voice.calibration import (
    WizardJobState,
    WizardProgressTracker,
    WizardStatus,
)
from sovyx.voice.calibration import _wizard_orchestrator as wo


_SLOW_READ_SECONDS = 0.20
_SENTINEL_TICK_S = 0.01
_TAIL_OBSERVATION_S = 0.50


def _seed_state() -> WizardJobState:
    return WizardJobState(
        job_id="job-async-io",
        mind_id="default",
        status=WizardStatus.SLOW_PATH_DIAG,
        progress=0.5,
        current_stage_message="diag",
        created_at_utc="2026-05-09T00:00:00Z",
        updated_at_utc="2026-05-09T00:00:00Z",
        profile_path=None,
        triage_winner_hid=None,
        error_summary=None,
        fallback_reason=None,
        extras={},
    )


class TestTailPromptsFileYieldsOnSlowFS:
    """Slow read must not starve sibling coroutines."""

    @pytest.mark.asyncio()
    async def test_sibling_coroutine_advances_during_slow_read(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        prompts_file = tmp_path / "prompts.jsonl"
        # Materialise the file so the worker takes the read branch (not
        # the FileNotFoundError branch).
        prompts_file.write_text(
            '{"type":"speak","phrase":"hello"}\n',
            encoding="utf-8",
        )

        # Slow-down primitive: every read_text call blocks the calling
        # thread (the worker thread that asyncio.to_thread spawns).
        original_read_text = Path.read_text

        def _slow_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == prompts_file:
                time.sleep(_SLOW_READ_SECONDS)
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _slow_read_text)

        # Sentinel: count event-loop ticks while the tail loop is alive.
        # If the loop is starved, ticks_observed stays at 0 (or 1 — a
        # boundary tick depending on scheduling).
        ticks_observed = 0
        sentinel_running = True

        async def _sentinel() -> None:
            nonlocal ticks_observed
            while sentinel_running:
                await asyncio.sleep(_SENTINEL_TICK_S)
                ticks_observed += 1

        # Inject a small poll interval so the tail loop iterates fast
        # enough to meet the test-deadline.
        monkeypatch.setattr(wo, "_PROMPTS_POLL_INTERVAL_S", 0.05)

        orchestrator = wo.WizardOrchestrator(data_dir=tmp_path)
        state_holder: dict[str, WizardJobState] = {"state": _seed_state()}
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")

        sentinel_task = asyncio.create_task(_sentinel())
        tail_task = asyncio.create_task(
            orchestrator._tail_prompts_file(  # noqa: SLF001
                prompts_file=prompts_file,
                state_holder=state_holder,
                tracker=tracker,
            ),
        )

        try:
            await asyncio.sleep(_TAIL_OBSERVATION_S)
        finally:
            sentinel_running = False
            tail_task.cancel()
            try:
                await tail_task
            except asyncio.CancelledError:
                pass
            try:
                await sentinel_task
            except asyncio.CancelledError:
                pass

        # During 0.5 s of wall time, with 10 ms sentinel sleeps and a
        # 200 ms blocking read on a worker thread, the sentinel must
        # accumulate at least 5 ticks. If the read had blocked the event
        # loop instead of the worker, the sentinel would have been
        # paused for the full 200 ms each iteration (≤2-3 ticks total).
        assert ticks_observed >= 5, (
            f"Event loop appears starved during slow filesystem reads — "
            f"sentinel observed only {ticks_observed} ticks across "
            f"{_TAIL_OBSERVATION_S}s (expected >= 5). The tail loop is "
            f"likely blocking the event loop again (regressed F2-H03)."
        )

    @pytest.mark.asyncio()
    async def test_missing_file_does_not_raise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """exists()-then-read race collapse: missing file yields None, no exc."""
        prompts_file = tmp_path / "never-created.jsonl"
        monkeypatch.setattr(wo, "_PROMPTS_POLL_INTERVAL_S", 0.02)

        orchestrator = wo.WizardOrchestrator(data_dir=tmp_path)
        state_holder: dict[str, WizardJobState] = {"state": _seed_state()}
        tracker = WizardProgressTracker(tmp_path / "progress.jsonl")

        tail_task = asyncio.create_task(
            orchestrator._tail_prompts_file(  # noqa: SLF001
                prompts_file=prompts_file,
                state_holder=state_holder,
                tracker=tracker,
            ),
        )
        await asyncio.sleep(0.10)  # ~5 poll cycles
        tail_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await tail_task

        # The state was never mutated because the file never existed.
        assert state_holder["state"].extras == {}
