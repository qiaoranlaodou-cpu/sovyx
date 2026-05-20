"""Mission H4 §T3.2 + §3 F9 — `sovyx doctor resources` CLI unit tests.

Closure addendum tag v0.49.30: spec §10.1 lists this file with ≥14
tests; HEAD pre-v0.49.30 had zero coverage of the doctor_resources
subcommand. This file lands the F9 assertion (every cohort field
renders a non-empty operator hint) plus the full flag matrix
(--json / --cohort / --explain / --watch / --tracemalloc-snapshot)
plus the daemon-RPC vs in-process-fallback dispatch behaviour.

Anti-pattern compliance:
* #8 — assertions use ``type(...).__name__`` for xdist class identity.
* #10 — no monkeypatching of `_ensure_token`; CLI is daemon-RPC tested
  via patched :class:`DaemonClient`.
* #11 — patches go through ``patch.object(module, "attr")`` for stable
  resolution under module-rename refactors.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from sovyx.cli.commands import doctor as doctor_module
from sovyx.cli.main import app
from sovyx.observability._resource_registry import (
    _HEALTH_SNAPSHOT_FIELDS,
    reset_default_resource_registry,
)

runner = CliRunner()


def _stable_payload(*, breaker_engaged: bool = False) -> dict[str, Any]:
    """Build a deterministic resource payload mirroring the RPC shape."""
    breaker_state = {
        "rss_growth": breaker_engaged,
        "thread_count": False,
        "lock_dict_cardinality": False,
        "onnx_session": False,
        "exception_cohort": False,
    }
    return {
        "fields": {
            "process.rss_bytes": 123_456_789,
            "process.num_threads": 42,
            "asyncio.task_count": 7,
            "to_thread.pool_size": 4,
            "lock_dict.total_cardinality": 99,
            "onnx.session_count": 4,
            "gc.objects_count": 50_000,
            "tracemalloc.is_tracing": False,
            "exception_cohort.retained_bytes_estimate": 0,
        },
        "breaker_state": breaker_state,
        "heap_snapshot_manifest": [
            {
                "name": "heap-snapshot-1747000000.json",
                "size_bytes": 4096,
                "mtime": 1_747_000_000.0,
            },
        ],
        "thread_snapshot_manifest": [],
    }


class TestExplainFlag:
    """``--explain <field>`` — F9 falsifiability gate."""

    def setup_method(self) -> None:
        reset_default_resource_registry()

    def test_explain_renders_non_empty_hint_for_every_registered_field(self) -> None:
        """F9: every canonical SSoT field MUST render a non-empty hint."""
        for field in _HEALTH_SNAPSHOT_FIELDS:
            result = runner.invoke(
                app,
                ["doctor", "resources", "--explain", field],
            )
            assert result.exit_code == 0, f"--explain {field} exited {result.exit_code}"
            assert field in result.stdout, f"--explain {field}: stdout missing field name"
            # Hint body must be non-trivial — strip the field+section header.
            body = result.stdout.replace(field, "").strip()
            assert len(body) > 40, (
                f"--explain {field}: rendered body too short "
                f"({len(body)} chars); operator hint must be load-bearing"
            )

    def test_explain_unknown_field_falls_back_to_canonical_pointer(self) -> None:
        result = runner.invoke(
            app,
            ["doctor", "resources", "--explain", "made_up_field_xyz"],
        )
        assert result.exit_code == 0
        # remediation_for() fallback points at the canonical playbook.
        assert "docs/operations/resource-hygiene.md" in result.stdout

    def test_explain_json_emits_field_and_hint_keys(self) -> None:
        target = "to_thread.pool_size"
        result = runner.invoke(
            app,
            ["doctor", "resources", "--json", "--explain", target],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["field"] == target
        assert isinstance(payload["hint"], str)
        assert len(payload["hint"]) > 40


class TestDefaultRender:
    """Bare `sovyx doctor resources` — no flags."""

    def setup_method(self) -> None:
        reset_default_resource_registry()

    def test_renders_section_tables_when_rpc_unavailable(self) -> None:
        """Daemon unreachable → in-process fallback path is exercised."""
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(), "local")
            result = runner.invoke(app, ["doctor", "resources"])
        assert result.exit_code == 0
        # Section headers + the manifest table render.
        assert "Engine resources" in result.stdout
        assert "Cohort circuit-breaker state" in result.stdout
        assert "Diagnostic snapshots" in result.stdout
        assert "heap-snapshot-1747000000.json" in result.stdout

    def test_local_source_renders_a_visible_degradation_notice(self) -> None:
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(), "local")
            result = runner.invoke(app, ["doctor", "resources"])
        assert result.exit_code == 0
        assert "Daemon not reachable" in result.stdout

    def test_daemon_source_does_not_render_the_degradation_notice(self) -> None:
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(), "daemon")
            result = runner.invoke(app, ["doctor", "resources"])
        assert result.exit_code == 0
        assert "Daemon not reachable" not in result.stdout

    def test_engaged_breaker_renders_red_marker(self) -> None:
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(breaker_engaged=True), "daemon")
            result = runner.invoke(app, ["doctor", "resources"])
        assert result.exit_code == 0
        # The engaged axis is rendered as ENGAGED somewhere in the table.
        assert "ENGAGED" in result.stdout

    def test_json_flag_emits_full_payload_with_source_key(self) -> None:
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(), "daemon")
            result = runner.invoke(app, ["doctor", "resources", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["source"] == "daemon"
        assert "fields" in payload
        assert "breaker_state" in payload
        assert "heap_snapshot_manifest" in payload


class TestCohortFilter:
    """``--cohort <section>`` — scope the table to one section."""

    def setup_method(self) -> None:
        reset_default_resource_registry()

    def test_cohort_filter_includes_matching_section(self) -> None:
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(), "daemon")
            result = runner.invoke(
                app,
                ["doctor", "resources", "--cohort", "process"],
            )
        assert result.exit_code == 0
        assert "Engine resources — process" in result.stdout

    def test_cohort_filter_hides_breaker_table_under_scope(self) -> None:
        """Per spec §0 item 14: breaker table is full-snapshot only."""
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(), "daemon")
            result = runner.invoke(
                app,
                ["doctor", "resources", "--cohort", "process"],
            )
        assert result.exit_code == 0
        assert "Cohort circuit-breaker state" not in result.stdout

    def test_unknown_cohort_emits_known_list_warning(self) -> None:
        with patch.object(doctor_module, "_fetch_resource_payload") as fetch:
            fetch.return_value = (_stable_payload(), "daemon")
            result = runner.invoke(
                app,
                ["doctor", "resources", "--cohort", "nonexistent_cohort"],
            )
        assert result.exit_code == 0
        assert "No fields registered" in result.stdout
        assert "Known cohorts" in result.stdout


class TestWatchFlag:
    """``--watch`` — 5-second refresh loop with clean Ctrl-C."""

    def setup_method(self) -> None:
        reset_default_resource_registry()

    def test_watch_loops_until_keyboard_interrupt(self) -> None:
        """Watch loop renders, sleeps once, then exits clean on KeyboardInterrupt."""
        call_count = {"n": 0}

        def fake_sleep(_sec: float) -> None:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise KeyboardInterrupt
            # First sleep returns normally; second raises.

        with (
            patch.object(doctor_module, "_fetch_resource_payload") as fetch,
            patch.object(doctor_module.time, "sleep", fake_sleep),
        ):
            fetch.return_value = (_stable_payload(), "daemon")
            result = runner.invoke(app, ["doctor", "resources", "--watch"])
        assert result.exit_code == 0
        # The Ctrl-C breadcrumb is rendered on clean exit.
        assert "Watch interrupted" in result.stdout
        # Render path called at least twice — proves the loop iterated.
        assert fetch.call_count >= 2

    def test_watch_json_emits_payloads_per_tick(self) -> None:
        """--watch --json prints one JSON object per tick (newline-separated)."""

        def fake_sleep(_sec: float) -> None:
            raise KeyboardInterrupt

        with (
            patch.object(doctor_module, "_fetch_resource_payload") as fetch,
            patch.object(doctor_module.time, "sleep", fake_sleep),
        ):
            fetch.return_value = (_stable_payload(), "daemon")
            result = runner.invoke(
                app,
                ["doctor", "resources", "--watch", "--json"],
            )
        assert result.exit_code == 0
        # First JSON object must parse.
        first_brace = result.stdout.find("{")
        assert first_brace != -1
        # No partial newline edges; one full JSON object must round-trip.
        snippet = result.stdout[first_brace:]
        # The output contains ≥ 1 JSON object; pull the first one safely.
        depth = 0
        end_idx = None
        for idx, ch in enumerate(snippet):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = idx + 1
                    break
        assert end_idx is not None
        payload = json.loads(snippet[:end_idx])
        assert payload["source"] == "daemon"


class TestTracemallocSnapshotFlag:
    """``--tracemalloc-snapshot`` — RPC trigger path."""

    def setup_method(self) -> None:
        reset_default_resource_registry()

    def test_no_daemon_renders_actionable_hint(self) -> None:
        class _NoDaemonClient:
            def is_daemon_running(self) -> bool:
                return False

        with patch.object(doctor_module, "DaemonClient", _NoDaemonClient):
            result = runner.invoke(
                app,
                ["doctor", "resources", "--tracemalloc-snapshot"],
            )
        assert result.exit_code == 0
        assert "Daemon not reachable" in result.stdout

    def test_no_daemon_json_emits_skipped_payload(self) -> None:
        class _NoDaemonClient:
            def is_daemon_running(self) -> bool:
                return False

        with patch.object(doctor_module, "DaemonClient", _NoDaemonClient):
            result = runner.invoke(
                app,
                ["doctor", "resources", "--tracemalloc-snapshot", "--json"],
            )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["skipped"] is True
        assert payload["reason"] == "daemon_not_running"

    def test_daemon_responds_skipped_propagates_hint(self) -> None:
        class _FakeClient:
            def is_daemon_running(self) -> bool:
                return True

            async def call(self, method: str, *args: object, **kwargs: object) -> Any:
                assert method == "engine.resources.tracemalloc_snapshot"
                return {
                    "skipped": True,
                    "reason": "tracemalloc_not_enabled_or_persist_failed",
                    "hint": "Enable SOVYX_OBSERVABILITY__FEATURES__TRACEMALLOC=true",
                }

        with patch.object(doctor_module, "DaemonClient", _FakeClient):
            result = runner.invoke(
                app,
                ["doctor", "resources", "--tracemalloc-snapshot"],
            )
        assert result.exit_code == 0
        assert "Heap snapshot skipped" in result.stdout
        assert "SOVYX_OBSERVABILITY__FEATURES__TRACEMALLOC" in result.stdout

    def test_daemon_success_renders_written_path(self) -> None:
        class _FakeClient:
            def is_daemon_running(self) -> bool:
                return True

            async def call(self, method: str, *args: object, **kwargs: object) -> Any:
                assert method == "engine.resources.tracemalloc_snapshot"
                return {
                    "skipped": False,
                    "name": "heap-snapshot-1747100000.json",
                    "path": "/tmp/heap-snapshot-1747100000.json",
                }

        with patch.object(doctor_module, "DaemonClient", _FakeClient):
            result = runner.invoke(
                app,
                ["doctor", "resources", "--tracemalloc-snapshot"],
            )
        assert result.exit_code == 0
        assert "Heap snapshot written" in result.stdout
        assert "heap-snapshot-1747100000.json" in result.stdout

    def test_daemon_success_json_round_trips(self) -> None:
        payload_result = {
            "skipped": False,
            "name": "heap-snapshot-1747200000.json",
            "path": "/tmp/x.json",
        }

        class _FakeClient:
            def is_daemon_running(self) -> bool:
                return True

            async def call(self, method: str, *args: object, **kwargs: object) -> Any:
                return payload_result

        with patch.object(doctor_module, "DaemonClient", _FakeClient):
            result = runner.invoke(
                app,
                ["doctor", "resources", "--tracemalloc-snapshot", "--json"],
            )
        assert result.exit_code == 0
        echoed = json.loads(result.stdout)
        assert echoed == payload_result


class TestLocalPayloadCollector:
    """``_collect_resource_payload_local`` — fallback used when daemon is absent."""

    def setup_method(self) -> None:
        reset_default_resource_registry()

    def test_local_payload_returns_all_top_level_keys(self) -> None:
        payload = doctor_module._collect_resource_payload_local()
        assert set(payload) >= {
            "fields",
            "breaker_state",
            "heap_snapshot_manifest",
            "thread_snapshot_manifest",
        }

    def test_local_payload_breaker_state_covers_every_axis(self) -> None:
        payload = doctor_module._collect_resource_payload_local()
        breaker = payload["breaker_state"]
        # Every CohortAxis member must be present (5 cohorts).
        assert len(breaker) == 5
        # All cohorts default to clear (False) on a freshly-reset registry.
        assert all(value is False for value in breaker.values())
