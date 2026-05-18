#!/usr/bin/env python3
"""Quality Gate 12 — LLM provider wire-discipline enforcement.

Mission C6 §T1.3 — anti-pattern #44 (dependency-gated workers MUST verify
their dependency contract end-to-end). Closes the 5-surface drift class
that allowed ``XGROK_API_KEY`` to ship pre-v0.42.x with the env-var
correctly wired in ``bootstrap.py`` but missing from the
``_ENV_VAR_MAP`` dict — silent registration failure on operators with
xAI keys configured.

This checker imports :class:`sovyx.llm._provider_registry.LLMProviderKey`
as the canonical source of truth and verifies every member is wired in
every applicable downstream consumer surface. Surface coverage ramps up
across the Mission C6 staged-adoption phases:

* v0.49.0 (Phase 1.A): surfaces 1 + 2 + 4 + 5.
* v0.49.2 (Phase 1.C): surface 3 added when i18n keys ship.
* v0.50.0 (Phase 3): all 5 surfaces; Gate 12 promotes to STRICT in
  ``scripts/verify_gates.sh`` (LENIENT through v0.49.x).

Surfaces:

1. ``src/sovyx/engine/bootstrap.py`` — for each cloud member, the env-var
   string MUST appear at least once. Liberal grep tolerates both the
   pre-T2.1 sequential-block shape and the post-T2.1 ``_PROVIDER_FACTORY``
   data-driven shape.
2. ``src/sovyx/dashboard/routes/onboarding.py`` — for each cloud member,
   the env-var string MUST appear at least once (in the legacy
   ``_ENV_VAR_MAP`` dict or the post-T1.1 ``env_var_map()`` import).
3. ``dashboard/src/locales/{en,pt-BR,es}/voice.json`` — for each member,
   the ``degraded.llm.providers.<name>.label`` AND
   ``degraded.llm.providers.<name>.envVar`` keys MUST exist (added in
   Phase 1.C; skipped earlier when the namespace doesn't yet exist).
4. ``src/sovyx/dashboard/routes/onboarding.py::_default_model_for`` —
   for each member, an entry in the ``defaults`` dict MUST exist.
5. ``docs/configuration.md`` — for each cloud member, the env-var string
   MUST appear in the document.

Allowlist mechanism: inline ``# c6-allowlist: <rationale>`` comments in
``_provider_registry.py`` exempt specific members from specific surfaces.
Format: ``# c6-allowlist: <surface_id>:<rationale>`` on the line that
declares the enum member.

Exit codes:
    0 — every applicable surface check passes for every member.
    1 — at least one surface drift detected.

Usage:

    uv run python scripts/dev/check_llm_provider_discipline.py
    uv run python scripts/dev/check_llm_provider_discipline.py --json
    uv run python scripts/dev/check_llm_provider_discipline.py \\
        --registry-path /tmp/_provider_registry_drifted.py

Invoked from ``scripts/verify_gates.sh`` as Gate 12 (LENIENT in Phase
1.A v0.49.0; STRICT in Phase 3 v0.50.0 per ADR-D12) AND from
``.github/workflows/publish.yml`` post-build verify (STRICT from day one;
publish path is bypass-proof).
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_REGISTRY_PATH = _REPO_ROOT / "src" / "sovyx" / "llm" / "_provider_registry.py"

_BOOTSTRAP_PATH = _REPO_ROOT / "src" / "sovyx" / "engine" / "bootstrap.py"
_ONBOARDING_PATH = _REPO_ROOT / "src" / "sovyx" / "dashboard" / "routes" / "onboarding.py"
_CONFIGURATION_DOC_PATH = _REPO_ROOT / "docs" / "configuration.md"
_LOCALE_DIRS = (
    _REPO_ROOT / "dashboard" / "src" / "locales" / "en",
    _REPO_ROOT / "dashboard" / "src" / "locales" / "pt-BR",
    _REPO_ROOT / "dashboard" / "src" / "locales" / "es",
)


@dataclass
class SurfaceFinding:
    surface_id: str
    member_name: str
    detail: str


@dataclass
class GateReport:
    members: tuple[str, ...]
    cloud_members: tuple[str, ...]
    env_vars: dict[str, str]
    findings: list[SurfaceFinding] = field(default_factory=list)
    surfaces_checked: list[str] = field(default_factory=list)
    surfaces_skipped: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings


def _load_registry(registry_path: Path) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Import the registry module dynamically and return ``(env_vars, allowlist)``.

    ``env_vars``: ``{member_value: env_var_string}``.
    ``allowlist``: ``{member_value: {surface_id, ...}}`` — surfaces this
    member is exempted from via inline ``# c6-allowlist:`` comments.
    """
    if not registry_path.is_file():
        msg = f"Registry path does not exist: {registry_path}"
        raise FileNotFoundError(msg)

    spec = importlib.util.spec_from_file_location("_c6_registry_check", registry_path)
    if spec is None or spec.loader is None:
        msg = f"Cannot load registry module from {registry_path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    key_cls = getattr(module, "LLMProviderKey", None)
    if key_cls is None:
        msg = "LLMProviderKey enum not found in registry"
        raise AttributeError(msg)
    env_vars: dict[str, str] = {member.value: member.env_var for member in key_cls}

    allowlist: dict[str, set[str]] = {member: set() for member in env_vars}
    source = registry_path.read_text(encoding="utf-8")
    allowlist_re = re.compile(
        r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]+)".*?#\s*c6-allowlist:\s*([^\s].*)$',
        re.MULTILINE,
    )
    for match in allowlist_re.finditer(source):
        member_value = match.group(2)
        directive = match.group(3).strip()
        for token in directive.split(","):
            surface_id = token.strip().split(":", 1)[0].strip()
            if surface_id and member_value in allowlist:
                allowlist[member_value].add(surface_id)

    return env_vars, allowlist


def _check_bootstrap_surface(report: GateReport, allowlist: dict[str, set[str]]) -> None:
    surface_id = "bootstrap"
    if not _BOOTSTRAP_PATH.is_file():
        report.surfaces_skipped.append(f"{surface_id} (file not present)")
        return
    report.surfaces_checked.append(surface_id)
    content = _BOOTSTRAP_PATH.read_text(encoding="utf-8")
    for member, env_var in report.env_vars.items():
        if not env_var:
            continue
        if surface_id in allowlist.get(member, set()):
            continue
        if env_var not in content:
            report.findings.append(
                SurfaceFinding(
                    surface_id=surface_id,
                    member_name=member,
                    detail=(
                        f"env-var '{env_var}' not found in bootstrap.py — "
                        f"provider '{member}' will never register."
                    ),
                ),
            )


def _check_onboarding_envvar_surface(report: GateReport, allowlist: dict[str, set[str]]) -> None:
    surface_id = "onboarding_envvar"
    if not _ONBOARDING_PATH.is_file():
        report.surfaces_skipped.append(f"{surface_id} (file not present)")
        return
    report.surfaces_checked.append(surface_id)
    content = _ONBOARDING_PATH.read_text(encoding="utf-8")
    for member, env_var in report.env_vars.items():
        if not env_var:
            continue
        if surface_id in allowlist.get(member, set()):
            continue
        if env_var not in content:
            report.findings.append(
                SurfaceFinding(
                    surface_id=surface_id,
                    member_name=member,
                    detail=(
                        f"env-var '{env_var}' not found in onboarding.py — "
                        f"dashboard onboarding cannot accept '{member}' keys."
                    ),
                ),
            )


def _check_onboarding_default_model_surface(
    report: GateReport,
    allowlist: dict[str, set[str]],
) -> None:
    surface_id = "default_model_for"
    if not _ONBOARDING_PATH.is_file():
        report.surfaces_skipped.append(f"{surface_id} (file not present)")
        return
    tree = ast.parse(_ONBOARDING_PATH.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_default_model_for":
            for assign in ast.walk(node):
                if isinstance(assign, ast.Dict):
                    for key_node in assign.keys:
                        if isinstance(key_node, ast.Constant) and isinstance(
                            key_node.value,
                            str,
                        ):
                            keys.add(key_node.value)
    if not keys:
        report.surfaces_skipped.append(f"{surface_id} (function or dict not found)")
        return
    report.surfaces_checked.append(surface_id)
    for member in report.env_vars:
        if surface_id in allowlist.get(member, set()):
            continue
        if member not in keys:
            report.findings.append(
                SurfaceFinding(
                    surface_id=surface_id,
                    member_name=member,
                    detail=(
                        f"'{member}' missing from _default_model_for defaults dict — "
                        f"onboarding-flow default-model resolution will return empty."
                    ),
                ),
            )


def _check_configuration_doc_surface(
    report: GateReport,
    allowlist: dict[str, set[str]],
) -> None:
    surface_id = "configuration_doc"
    if not _CONFIGURATION_DOC_PATH.is_file():
        report.surfaces_skipped.append(f"{surface_id} (file not present)")
        return
    report.surfaces_checked.append(surface_id)
    content = _CONFIGURATION_DOC_PATH.read_text(encoding="utf-8")
    for member, env_var in report.env_vars.items():
        if not env_var:
            continue
        if surface_id in allowlist.get(member, set()):
            continue
        if env_var not in content:
            report.findings.append(
                SurfaceFinding(
                    surface_id=surface_id,
                    member_name=member,
                    detail=(
                        f"env-var '{env_var}' not documented in docs/configuration.md — "
                        f"operators searching the docs for '{member}' setup find nothing."
                    ),
                ),
            )


def _check_locale_surface(report: GateReport, allowlist: dict[str, set[str]]) -> None:
    """Surface 3 — degraded.llm.providers.<member>.{label,envVar} keys.

    Skipped when the ``degraded.llm.providers`` namespace doesn't yet exist
    (Phase 1.A foundation ships before Phase 1.C i18n keys). When the
    namespace exists in ANY locale, the check expects it in ALL three.
    """
    surface_id = "locale_providers"

    namespace_present: dict[str, dict] = {}
    for locale_dir in _LOCALE_DIRS:
        voice_path = locale_dir / "voice.json"
        if not voice_path.is_file():
            continue
        try:
            payload = json.loads(voice_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        providers_namespace = payload.get("degraded", {}).get("llm", {}).get("providers")
        if isinstance(providers_namespace, dict):
            namespace_present[locale_dir.name] = providers_namespace

    if not namespace_present:
        report.surfaces_skipped.append(
            f"{surface_id} (degraded.llm.providers namespace not yet present in any locale; "
            "expected pre-Phase-1.C v0.49.2 — will be enforced once Phase 1.C ships)",
        )
        return

    report.surfaces_checked.append(surface_id)
    expected_locales = {dir_.name for dir_ in _LOCALE_DIRS if (dir_ / "voice.json").is_file()}
    missing_locales = expected_locales - set(namespace_present.keys())
    if missing_locales:
        missing_list = ", ".join(sorted(missing_locales))
        report.findings.append(
            SurfaceFinding(
                surface_id=surface_id,
                member_name="(namespace)",
                detail=(
                    "degraded.llm.providers namespace present in some locales "
                    f"but missing in: {missing_list}. "
                    "ADR-D7 requires all 3 locales same commit."
                ),
            ),
        )
        return

    for member in report.env_vars:
        if surface_id in allowlist.get(member, set()):
            continue
        for locale_name, providers in namespace_present.items():
            entry = providers.get(member)
            if not isinstance(entry, dict):
                report.findings.append(
                    SurfaceFinding(
                        surface_id=surface_id,
                        member_name=member,
                        detail=(
                            f"locale '{locale_name}' missing "
                            f"degraded.llm.providers.{member} entry."
                        ),
                    ),
                )
                continue
            for required_key in ("label", "envVar"):
                if required_key not in entry:
                    report.findings.append(
                        SurfaceFinding(
                            surface_id=surface_id,
                            member_name=member,
                            detail=(
                                f"locale '{locale_name}' missing "
                                f"degraded.llm.providers.{member}.{required_key}."
                            ),
                        ),
                    )


def _build_report(registry_path: Path) -> GateReport:
    env_vars, allowlist = _load_registry(registry_path)
    cloud_members = tuple(member for member, env_var in env_vars.items() if env_var)
    report = GateReport(
        members=tuple(env_vars.keys()),
        cloud_members=cloud_members,
        env_vars=env_vars,
    )
    _check_bootstrap_surface(report, allowlist)
    _check_onboarding_envvar_surface(report, allowlist)
    _check_onboarding_default_model_surface(report, allowlist)
    _check_configuration_doc_surface(report, allowlist)
    _check_locale_surface(report, allowlist)
    return report


def _report_to_dict(report: GateReport) -> dict[str, object]:
    return {
        "members": list(report.members),
        "cloud_members": list(report.cloud_members),
        "env_vars": dict(report.env_vars),
        "surfaces_checked": list(report.surfaces_checked),
        "surfaces_skipped": list(report.surfaces_skipped),
        "findings": [
            {
                "surface_id": finding.surface_id,
                "member_name": finding.member_name,
                "detail": finding.detail,
            }
            for finding in report.findings
        ],
        "passed": report.passed,
    }


def _print_human(report: GateReport) -> None:
    if report.passed:
        ok_count = len(report.surfaces_checked)
        skip_count = len(report.surfaces_skipped)
        print(
            f"Quality Gate 12 — LLM provider discipline: PASS "
            f"({len(report.members)} members; {ok_count} surfaces checked, "
            f"{skip_count} surfaces skipped).",
        )
        for skipped in report.surfaces_skipped:
            print(f"  • skipped: {skipped}")
        return

    print(
        "Quality Gate 12 — LLM provider discipline: FAILED.",
        file=sys.stderr,
    )
    print(
        f"  members           = {len(report.members)} "
        f"({len(report.cloud_members)} cloud + Ollama)",
        file=sys.stderr,
    )
    print(
        f"  surfaces checked  = {', '.join(report.surfaces_checked) or '(none)'}",
        file=sys.stderr,
    )
    if report.surfaces_skipped:
        print(
            f"  surfaces skipped  = {', '.join(report.surfaces_skipped)}",
            file=sys.stderr,
        )
    print(f"  drift findings    = {len(report.findings)}", file=sys.stderr)
    for finding in report.findings:
        print(
            f"    ✗ [{finding.surface_id}] {finding.member_name}: {finding.detail}",
            file=sys.stderr,
        )
    print(
        "\nAnti-pattern #44 enforcement (Mission C6): every LLMProviderKey member "
        "MUST be wired across every applicable downstream consumer surface so a "
        "new 10th-or-Nth provider cannot ship with silent registration failure. "
        "Allowlist a deliberate exemption with an inline "
        "'# c6-allowlist: <surface_id>:<rationale>' comment on the enum-member line.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Quality Gate 12 — LLM provider wire-discipline (Mission C6).",
    )
    parser.add_argument(
        "--registry-path",
        type=Path,
        default=_DEFAULT_REGISTRY_PATH,
        help="Path to the LLMProviderKey registry module (default: %(default)s).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON report on stdout instead of human-readable output.",
    )
    args = parser.parse_args(argv)

    report = _build_report(args.registry_path)

    if args.json:
        print(json.dumps(_report_to_dict(report), indent=2, sort_keys=True))
    else:
        _print_human(report)

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
