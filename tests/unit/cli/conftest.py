"""Shared test fixtures for ``tests/unit/cli/``.

Closes ``GAPS-CONSOLIDATED-2026-05-13.md`` §4.3 (test hermeticity for
CLI tests that patch ``Path.home`` to ``tmp_path`` but don't seed
mind state).

The :func:`_seed_default_mind` helper writes the minimum-viable
``<sovyx_data>/default/mind.yaml`` so the mind resolver (Phase 1.T1
of MISSION-voice-config-calibrate-enterprise) auto-detects
``'default'`` without needing the runner's real ``~/.sovyx``.

The :func:`_seed_tmp_path_default_mind` autouse fixture seeds the
per-test ``tmp_path`` BEFORE the test runs. Tests that subsequently
patch ``sovyx.cli.commands.doctor.Path.home`` or
``sovyx.cli.main.Path.home`` to ``tmp_path`` find the seeded mind
config immediately — covers the Agent #2 audit's MEDIUM-risk class:
"safe today, fragile to future change" (a future doctor / init code
path that invokes the resolver would silently break the affected
tests without this seed).

Tests that want a non-default mind seeded MUST call
:func:`_seed_default_mind` against their own data dir before the
CLI invocation; the autouse fixture only seeds at the per-test
``tmp_path``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _seed_default_mind(sovyx_data: Path) -> None:
    """Create ``<sovyx_data>/default/mind.yaml`` so ``resolve_mind_id``
    auto-detects ``'default'`` for tests that operate on the legacy
    mind name.

    The seeded mind.yaml carries a non-empty
    ``voice_input_device_name`` so the v0.40.0 STRICT calibrate
    prereq gate is a silent no-op for tests that don't explicitly
    exercise it.

    Mirrors ``tests/unit/cli/test_doctor_calibrate.py::_seed_default_mind``
    — kept in conftest as the canonical helper.
    """
    default_dir = sovyx_data / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "mind.yaml").write_text(
        "name: default\nid: default\nvoice_input_device_name: stub-mic\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _seed_tmp_path_default_mind(tmp_path: Path) -> None:
    """Pre-seed ``tmp_path/.sovyx/default/mind.yaml`` before each test.

    Tests that don't patch ``Path.home`` to ``tmp_path`` are
    unaffected (the seeded directory simply lives unused in
    ``tmp_path``). Tests that DO patch ``Path.home`` to ``tmp_path``
    automatically inherit a configured ``default`` mind — closing
    the v0.39.1-class CI flake on clean runners (anti-pattern #23).
    """
    _seed_default_mind(tmp_path / ".sovyx")
