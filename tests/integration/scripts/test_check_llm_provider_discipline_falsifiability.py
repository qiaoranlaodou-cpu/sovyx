"""Falsifiability test — Quality Gate 12 must reject synthetic drift (Mission C6 §F1, §T1.7).

Pre-mission HEAD has no Gate 12 script; post-mission Phase 1.A the gate
EXISTS and PASSES at HEAD. This test creates a tmpfs copy of
``_provider_registry.py`` with an extra synthetic member that has no
parallel wiring in any consumer surface, runs Gate 12 against that
drifted registry, and asserts the gate emits exit≠0 + a "synthetic"
finding.

If F1 fails, the gate's AST scanner is too lenient — extend the matcher.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GATE_SCRIPT = _REPO_ROOT / "scripts" / "dev" / "check_llm_provider_discipline.py"
_REGISTRY_SRC = _REPO_ROOT / "src" / "sovyx" / "llm" / "_provider_registry.py"


@pytest.mark.skipif(
    not _GATE_SCRIPT.is_file(),
    reason="Gate 12 script not present (pre-Mission-C6 HEAD).",
)
class TestGate12Falsifiability:
    def test_pre_mission_baseline_passes(self) -> None:
        """Gate 12 PASSES at HEAD post-Mission-C6 Phase 1.A."""
        result = subprocess.run(
            [sys.executable, str(_GATE_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Gate 12 must pass at HEAD; got exit={result.returncode}. "
            f"stdout={result.stdout}, stderr={result.stderr}"
        )
        assert "PASS" in (result.stdout + result.stderr)

    def test_synthetic_drift_fails_gate12(self, tmp_path: Path) -> None:
        """F1 falsifiability — an 11th member with no wiring MUST fail Gate 12."""
        drifted = tmp_path / "_provider_registry_drifted.py"
        shutil.copy(_REGISTRY_SRC, drifted)
        original = drifted.read_text(encoding="utf-8")
        injection = '\n    SYNTHETIC = "synthetic"'
        new_content = original.replace(
            'OLLAMA = "ollama"',
            f'OLLAMA = "ollama"{injection}',
        )
        # Also extend the env-var + default-model maps so the registry imports
        # without KeyError; the wire-discipline gap is the test target.
        new_content = new_content.replace(
            'LLMProviderKey.OLLAMA: "",\n}\n\n\n# Conservative',
            (
                'LLMProviderKey.OLLAMA: "",\n'
                '    LLMProviderKey.SYNTHETIC: "SYNTHETIC_API_KEY",\n'
                "}\n\n\n# Conservative"
            ),
        )
        new_content = new_content.replace(
            'LLMProviderKey.OLLAMA: "",\n}\n\n\n__all__',
            (
                'LLMProviderKey.OLLAMA: "",\n'
                '    LLMProviderKey.SYNTHETIC: "synthetic-model",\n'
                "}\n\n\n__all__"
            ),
        )
        drifted.write_text(new_content, encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(_GATE_SCRIPT),
                "--registry-path",
                str(drifted),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, (
            f"Gate 12 MUST fail on synthetic drift; got exit={result.returncode}. "
            f"stdout={result.stdout}, stderr={result.stderr}"
        )
        combined = (result.stdout + result.stderr).lower()
        assert "synthetic" in combined, (
            f"Gate 12 failure output should mention 'synthetic'; got: {combined}"
        )

    def test_synthetic_drift_json_output_lists_findings(self, tmp_path: Path) -> None:
        """--json mode emits structured findings the analyzer can parse."""
        import json

        drifted = tmp_path / "_provider_registry_drifted.py"
        shutil.copy(_REGISTRY_SRC, drifted)
        content = drifted.read_text(encoding="utf-8")
        content = content.replace(
            'OLLAMA = "ollama"',
            'OLLAMA = "ollama"\n    SYNTHETIC = "synthetic"',
        )
        content = content.replace(
            'LLMProviderKey.OLLAMA: "",\n}\n\n\n# Conservative',
            (
                'LLMProviderKey.OLLAMA: "",\n'
                '    LLMProviderKey.SYNTHETIC: "SYNTHETIC_API_KEY",\n'
                "}\n\n\n# Conservative"
            ),
        )
        content = content.replace(
            'LLMProviderKey.OLLAMA: "",\n}\n\n\n__all__',
            (
                'LLMProviderKey.OLLAMA: "",\n'
                '    LLMProviderKey.SYNTHETIC: "synthetic-model",\n'
                "}\n\n\n__all__"
            ),
        )
        drifted.write_text(content, encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(_GATE_SCRIPT),
                "--registry-path",
                str(drifted),
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        # JSON goes to stdout regardless of pass/fail
        payload = json.loads(result.stdout)
        assert payload["passed"] is False
        assert any(finding["member_name"] == "synthetic" for finding in payload["findings"])
