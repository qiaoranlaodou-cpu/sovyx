"""Unit tests for Mission H4 §T1.4 — Quality Gate 15 AST scanner.

Verifies the four invariant checks against synthetic source-text
inputs written to a tmp_path-scoped scan root.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.dev.check_resource_hygiene_discipline import (
    run_check,
)


def _build_synthetic_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Materialize a synthetic ``src/sovyx/...`` tree under tmp_path."""
    for rel_path, content in files.items():
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content), encoding="utf-8")
    return tmp_path / "src" / "sovyx"


# ── Producer parity (invariant #1) ──


class TestProducerParity:
    def test_unknown_emit_field_flagged(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/bad_emit.py": '''
                """Synthetic producer with an unknown field."""
                from sovyx.observability.logging import get_logger
                logger = get_logger(__name__)

                def emit() -> None:
                    logger.info(
                        "self.health.snapshot",
                        **{"made_up_field": 42},
                    )
                ''',
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        kinds = {v.kind for v in report.violations}
        assert "producer_unknown_field" in kinds

    def test_known_emit_field_not_flagged(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/ok_emit.py": """
                from sovyx.observability.logging import get_logger
                logger = get_logger(__name__)

                def emit() -> None:
                    logger.info(
                        "self.health.snapshot",
                        **{"process.rss_bytes": 12345},
                    )
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        producer_violations = [v for v in report.violations if v.kind == "producer_unknown_field"]
        assert producer_violations == []

    def test_internal_metadata_keys_not_flagged(self, tmp_path: Path) -> None:
        """``self.health.snapshot_final`` etc. are emitter-internal — allowed."""
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/internal_emit.py": """
                from sovyx.observability.logging import get_logger
                logger = get_logger(__name__)

                def emit() -> None:
                    logger.info(
                        "self.health.snapshot",
                        **{"self.health.snapshot_final": True, "self.health.uptime_s": 1.0},
                    )
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        assert report.violations == []


# ── Consumer parity (invariant #2) ──


class TestConsumerParity:
    def test_drift_consumer_flagged(self, tmp_path: Path) -> None:
        """``event_dict.get("system.rss_bytes")`` is a legacy alias — must still resolve.

        After Phase 1.B the legacy alias is the documented value of
        ``process.rss_bytes``'s ``FieldSpec.legacy_alias``. The scanner
        treats it as compliant via the alias set.
        """
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/consumer.py": """
                def f(event_dict: dict) -> None:
                    _ = event_dict.get("system.rss_bytes")
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        assert [v for v in report.violations if v.kind == "consumer_unknown_field"] == []

    def test_truly_unknown_consumer_flagged(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/consumer_bad.py": """
                def f(event_dict: dict) -> None:
                    _ = event_dict.get("process.totally_made_up_field")
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        kinds = {v.kind for v in report.violations}
        assert "consumer_unknown_field" in kinds

    def test_non_snapshot_get_not_flagged(self, tmp_path: Path) -> None:
        """``event_dict.get("plain_key")`` outside the snapshot field heuristic — allowed."""
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/other_consumer.py": """
                def f(event_dict: dict) -> None:
                    _ = event_dict.get("user.id")
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        assert report.violations == []


# ── ONNX construction-site pairing (invariant #3a) ──


class TestOnnxPairing:
    def test_unpaired_onnx_flagged(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/voice/synth_no_pair.py": """
                import onnxruntime as ort
                def setup() -> None:
                    sess = ort.InferenceSession("model.onnx")
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        kinds = {v.kind for v in report.violations}
        assert "onnx_unpaired" in kinds

    def test_paired_onnx_not_flagged(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/voice/synth_paired.py": """
                import onnxruntime as ort
                from sovyx.observability import register_onnx_session

                def setup() -> None:
                    sess = ort.InferenceSession("model.onnx")
                    register_onnx_session(label="voice.synth", session=sess)
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        assert [v for v in report.violations if v.kind == "onnx_unpaired"] == []

    def test_allowlist_skips_onnx_pairing(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/voice/synth_allowlisted.py": """
                import onnxruntime as ort
                def setup() -> None:
                    sess = ort.InferenceSession("model.onnx")  # h4-allowlist: out-of-process
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        assert [v for v in report.violations if v.kind == "onnx_unpaired"] == []


# ── LRULockDict construction-site pairing (invariant #3b) ──


class TestLockDictPairing:
    def test_unpaired_lockdict_flagged(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/lockdict_no_pair.py": """
                from sovyx.engine._lock_dict import LRULockDict
                def setup() -> None:
                    locks = LRULockDict(maxsize=128)
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        kinds = {v.kind for v in report.violations}
        assert "lockdict_unpaired" in kinds

    def test_paired_lockdict_not_flagged(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/lockdict_paired.py": """
                from sovyx.engine._lock_dict import LRULockDict
                from sovyx.observability import register_lock_dict
                def setup() -> None:
                    locks = LRULockDict(maxsize=128)
                    register_lock_dict(owner_id="foo.bar", dict_ref=locks)
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        assert [v for v in report.violations if v.kind == "lockdict_unpaired"] == []


# ── asyncio.to_thread informational (invariant #4) ──


class TestToThreadInformational:
    def test_bare_to_thread_informational_only(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/legacy_to_thread.py": """
                import asyncio
                async def f() -> int:
                    return await asyncio.to_thread(lambda: 42)
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        # No violations (LENIENT informational).
        assert report.violations == []
        assert any(v.kind == "future_migration" for v in report.informational)

    def test_allowlisted_to_thread_not_in_informational(self, tmp_path: Path) -> None:
        scan_root = _build_synthetic_tree(
            tmp_path,
            {
                "src/sovyx/foo/bootstrap_to_thread.py": """
                import asyncio
                async def f() -> int:
                    return await asyncio.to_thread(lambda: 42)  # h4-allowlist: lifecycle-bootstrap
                """,
            },
        )
        report = run_check(scan_root, repo_root=tmp_path)
        assert report.violations == []
        assert report.informational == []
