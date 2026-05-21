"""F-012 regression — MISSION-A.2.P4 open_files_count triple-None disambiguation.

Mission anchor:
``docs-internal/missions/MISSION-A2-operator-trust-remediation-2026-05-20.md``
§T4.1..T4.8.

Pre-fix the ``process.open_files_count`` / ``process.connections_count``
``None`` value collapsed three (actually four) distinct conditions:

  1. ``skip_expensive=True`` on the final shutdown snapshot.
  2. psutil raised PermissionError (Linux non-root, Windows ACL).
  3. psutil raised some other Exception (NoSuchProcess, OSError).
  4. psutil unavailable (ImportError).

Downstream consumers reading ``open_files_count = null`` could not
disambiguate "intentional skip" from "permission denied" from
"process gone" from "psutil missing".

Post-fix the parallel ``process.open_files_status`` /
``process.connections_status`` fields carry one of:

  - ``ok``               — count is live and accurate.
  - ``skipped_shutdown`` — intentional skip on shutdown snapshot.
  - ``denied``           — psutil raised PermissionError.
  - ``unsupported``      — other psutil exception (edge case).
  - ``psutil_missing``   — psutil not installed.

These tests exercise each branch via direct invocation of
``_capture_psutil_metrics`` + (for the unsupported / denied paths) a
``patch.object(psutil.Process, ...)`` stub.
"""

from __future__ import annotations

from unittest.mock import patch

from sovyx.observability.resources import _capture_psutil_metrics


class TestOpenFilesStatusDisambiguation:
    """F-012 — parallel ``_status`` field disambiguates the four None branches."""

    def test_ok_status_on_successful_psutil_call(self) -> None:
        """Happy path: ``status == 'ok'`` and count is a non-None int."""
        metrics = _capture_psutil_metrics(skip_expensive=False)
        assert metrics["process.open_files_status"] == "ok"
        assert metrics["process.connections_status"] == "ok"
        # Both counts MUST be int (or None on very-restricted hosts —
        # but on the test runner they're always int).
        assert isinstance(metrics["process.open_files_count"], int)
        assert isinstance(metrics["process.connections_count"], int)

    def test_skipped_shutdown_status_on_final_snapshot(self) -> None:
        """``skip_expensive=True`` (shutdown path) sets status='skipped_shutdown'."""
        metrics = _capture_psutil_metrics(skip_expensive=True)
        assert metrics["process.open_files_status"] == "skipped_shutdown"
        assert metrics["process.connections_status"] == "skipped_shutdown"
        # Counts are None when skipped.
        assert metrics["process.open_files_count"] is None
        assert metrics["process.connections_count"] is None

    def test_denied_status_on_permission_error(self) -> None:
        """PermissionError → status='denied' (Linux non-root common case)."""
        import psutil

        # Patch BOTH open_files + net_connections to raise PermissionError;
        # the disambiguation is per-call so each status field is independent.
        with (
            patch.object(
                psutil.Process,
                "open_files",
                side_effect=PermissionError("EACCES"),
            ),
            patch.object(
                psutil.Process,
                "net_connections",
                side_effect=PermissionError("EACCES"),
            ),
        ):
            metrics = _capture_psutil_metrics(skip_expensive=False)
        assert metrics["process.open_files_status"] == "denied"
        assert metrics["process.connections_status"] == "denied"
        assert metrics["process.open_files_count"] is None
        assert metrics["process.connections_count"] is None

    def test_unsupported_status_on_generic_psutil_exception(self) -> None:
        """Non-PermissionError psutil failure → status='unsupported'."""
        import psutil

        with (
            patch.object(
                psutil.Process,
                "open_files",
                side_effect=psutil.NoSuchProcess(pid=-1),
            ),
            patch.object(
                psutil.Process,
                "net_connections",
                side_effect=OSError("ENOTSUP"),
            ),
        ):
            metrics = _capture_psutil_metrics(skip_expensive=False)
        assert metrics["process.open_files_status"] == "unsupported"
        assert metrics["process.connections_status"] == "unsupported"
        assert metrics["process.open_files_count"] is None
        assert metrics["process.connections_count"] is None

    def test_psutil_missing_status_on_import_failure(self) -> None:
        """ImportError on psutil → status='psutil_missing' for both fields.

        Mocks ``builtins.__import__`` to raise ImportError for ``psutil``
        specifically; the dead-branch defense at ``resources.py:75-94``
        is exercised and the parallel status fields are wired.
        """
        import builtins

        real_import = builtins.__import__

        def _raising_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "psutil":
                raise ImportError("psutil unavailable (synthetic)")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_raising_import):
            metrics = _capture_psutil_metrics(skip_expensive=False)
        assert metrics["process.open_files_status"] == "psutil_missing"
        assert metrics["process.connections_status"] == "psutil_missing"
        assert metrics["process.open_files_count"] is None
        assert metrics["process.connections_count"] is None

    def test_status_field_in_ssot(self) -> None:
        """Both new status fields land as canonical SSoT entries (Gate 15)."""
        from sovyx.observability._resource_registry import _HEALTH_SNAPSHOT_FIELDS

        assert "process.open_files_status" in _HEALTH_SNAPSHOT_FIELDS
        assert "process.connections_status" in _HEALTH_SNAPSHOT_FIELDS
        ofs = _HEALTH_SNAPSHOT_FIELDS["process.open_files_status"]
        cs = _HEALTH_SNAPSHOT_FIELDS["process.connections_status"]
        assert ofs.type_constraint is str
        assert cs.type_constraint is str
        assert ofs.section == "process"
        assert cs.section == "process"


class TestStatusFieldOperatorHints:
    """F-012 — operator-hint registry includes the two new fields."""

    def test_open_files_status_has_remediation_hint(self) -> None:
        from sovyx.observability._resource_remediation import FIELD_REMEDIATIONS

        hint = FIELD_REMEDIATIONS.get("process.open_files_status")
        assert hint is not None
        # Must mention all 5 status values so operators can disambiguate.
        for status in ("ok", "skipped_shutdown", "denied", "unsupported", "psutil_missing"):
            assert status in hint, f"{status!r} not disclosed in operator hint"

    def test_connections_status_has_remediation_hint(self) -> None:
        from sovyx.observability._resource_remediation import FIELD_REMEDIATIONS

        hint = FIELD_REMEDIATIONS.get("process.connections_status")
        assert hint is not None
        # Connections-specific guidance: CAP_NET_ADMIN on Linux non-root.
        assert "CAP_NET_ADMIN" in hint
