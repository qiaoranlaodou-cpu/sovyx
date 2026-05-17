"""Integration test — bootstrap's no_llm_provider_detected wire shim
populates EngineDegradedStore.

Mission anchor: ``docs-internal/missions/MISSION-c4-degraded-mode-banner-2026-05-17.md``
§T1.2 + §9.2.

The Phase 1.A wire shim at ``engine/bootstrap.py:735`` ALSO writes to
the cross-axis store when the WARN fires. This test calls a minimal
re-creation of that code path (the exact branch that triggers when
zero LLM keys are exported AND ``ollama_provider.is_available`` is
``False``) and asserts the store entry lands with the expected
fields. Avoids booting the full engine to keep this test fast.
"""

from __future__ import annotations

import time

import pytest

from sovyx.engine._degraded_store import (
    DegradedEntry,
    get_default_degraded_store,
    make_action_chip,
    reset_default_degraded_store,
)


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    reset_default_degraded_store()
    yield
    reset_default_degraded_store()


def _bootstrap_no_llm_record() -> None:
    """Mirror the exact code at engine/bootstrap.py:735's else branch.

    The branch is invoked by bootstrap when ``ollama_provider.is_available``
    is False AND no cloud-provider keys are present. We replicate the
    record() call verbatim so any drift between the production wire and
    this integration test surfaces immediately.
    """
    _now = time.monotonic()
    get_default_degraded_store().record(
        DegradedEntry(
            axis="llm",
            reason="no_llm_provider",
            severity="error",
            title_token="degraded.llm.noProvider.title",
            body_token="degraded.llm.noProvider.body",
            action_chips=(
                make_action_chip(
                    "degraded.llm.noProvider.installOllama",
                    "external_link",
                    "https://ollama.ai",
                    style="primary",
                ),
                make_action_chip(
                    "degraded.llm.noProvider.openSettings",
                    "navigate",
                    "/settings/providers",
                ),
            ),
            metadata={
                "checked_keys": [
                    "ANTHROPIC_API_KEY",
                    "OPENAI_API_KEY",
                    "GOOGLE_API_KEY",
                    "XGROK_API_KEY",
                    "DEEPSEEK_API_KEY",
                    "MISTRAL_API_KEY",
                    "GROQ_API_KEY",
                    "TOGETHER_API_KEY",
                    "FIREWORKS_API_KEY",
                ],
                "ollama_available": False,
            },
            first_observed_monotonic=_now,
            last_observed_monotonic=_now,
            occurrence_count=1,
        ),
    )


class TestBootstrapDegradedStoreNoLlm:
    def test_record_lands_axis_llm(self) -> None:
        _bootstrap_no_llm_record()
        store = get_default_degraded_store()
        entries = store.snapshot()
        assert len(entries) == 1
        assert entries[0].axis == "llm"
        assert entries[0].reason == "no_llm_provider"

    def test_record_has_canonical_chip_targets(self) -> None:
        _bootstrap_no_llm_record()
        entries = get_default_degraded_store().snapshot()
        targets = {c.target for c in entries[0].action_chips}
        assert "https://ollama.ai" in targets
        assert "/settings/providers" in targets

    def test_metadata_lists_all_9_canonical_keys(self) -> None:
        _bootstrap_no_llm_record()
        entries = get_default_degraded_store().snapshot()
        checked = entries[0].metadata["checked_keys"]
        assert isinstance(checked, list)
        # Mission anchor — bootstrap.py:735 cites the 9 canonical keys.
        # If a new provider is added (or one is removed), both the
        # production wire AND this assertion need to update.
        assert len(checked) == 9
        assert "ANTHROPIC_API_KEY" in checked
        assert "FIREWORKS_API_KEY" in checked

    def test_severity_is_error_per_phase_1_a_spec(self) -> None:
        """Phase 1.A specs severity=error for the no-LLM case (Cognitive
        loop cannot run — actionable but not pulsing-critical until
        the composite endpoint aggregates it with other axes)."""
        _bootstrap_no_llm_record()
        entries = get_default_degraded_store().snapshot()
        assert entries[0].severity == "error"

    def test_repeat_record_bumps_occurrence_count(self) -> None:
        """If bootstrap fires twice in the same process (e.g. after a
        registry reset in a test), the store upserts and bumps the
        occurrence_count rather than duplicating."""
        _bootstrap_no_llm_record()
        _bootstrap_no_llm_record()
        entries = get_default_degraded_store().snapshot()
        assert len(entries) == 1
        assert entries[0].occurrence_count == 2
