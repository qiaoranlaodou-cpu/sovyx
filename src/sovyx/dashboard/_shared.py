"""Shared utilities for dashboard modules.

Contains common logic used across status, brain, and conversations modules
to avoid DRY violations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

    from sovyx.engine.registry import ServiceRegistry

logger = get_logger(__name__)


# ── Mind-id resolution sources (Mission §Phase 1 T1.2) ────────────
# Reported under ``voice.source`` in
# ``voice.dashboard.voice_enable_mind_resolved`` so dashboards can
# distinguish where the active mind came from. The string values are
# part of the public observability vocabulary; renaming any of them
# is a breaking change for Grafana panels keyed on the field.
MIND_ID_SOURCE_APP_STATE = "app_state"
MIND_ID_SOURCE_MIND_MANAGER = "mind_manager"
MIND_ID_SOURCE_FALLBACK_DEFAULT = "fallback_default"
MIND_ID_SOURCE_EXPLICIT_REQUEST = "explicit_request"


async def get_active_mind_id(registry: ServiceRegistry) -> str:
    """Get the first active mind ID from MindManager.

    Used by status, brain, and conversations modules for mind-scoped queries.
    Returns ``"default"`` if MindManager is unavailable OR registered but
    holding no active minds — see the fallback caveat below.

    .. note::

       Phase 1.T1.5 (v0.39.0) makes ``sovyx start`` refuse to boot
       without a real mind on disk, so :class:`MindManager` ALWAYS
       holds at least one active mind in production. The ``"default"``
       fallback below is therefore unreachable from a healthy
       production daemon. It is preserved for two legitimate states:

       * Test fixtures that exercise the dashboard surface without
         going through bootstrap (no MindManager registered).
       * The transient bootstrap window before the first mind is
         registered (microseconds-scale, but observable in tightly-
         timed startup races).

       Phase 6.T6.3 (v0.40.1) wires a structured WARN
       ``dashboard.shared.fallback_default_mind`` at the fallback
       point so any production occurrence surfaces as a grep-able
       signal — operators can wire alerts to flag a regression of
       the Phase 1.T1.5 daemon-boot gate.
    """
    try:
        from sovyx.engine.bootstrap import MindManager

        if registry.is_registered(MindManager):
            manager = await registry.resolve(MindManager)
            minds = manager.get_active_minds()
            if minds:
                return minds[0]
    except Exception:  # noqa: BLE001
        logger.debug("get_active_mind_id_failed")
    logger.warning(
        "dashboard.shared.fallback_default_mind",
        callsite="get_active_mind_id",
        reason=(
            "MindManager not registered OR returned no active minds; "
            "falling back to the literal 'default' sentinel. In a healthy "
            "production daemon this path is unreachable (Phase 1.T1.5 "
            "ensures sovyx start refuses to boot without a real mind) — "
            "any occurrence here flags either a test fixture, a transient "
            "bootstrap window, or a regression of the daemon-boot gate."
        ),
    )
    return "default"


async def resolve_active_mind_id_for_request(
    request: Request,
) -> tuple[str, str]:
    """Resolve the active mind id for a dashboard request.

    Resolution order (first hit wins):

    1. ``request.app.state.mind_id`` — the cached value the dashboard
       server populates at startup from
       :class:`sovyx.engine.bootstrap.MindManager`. Source string:
       :data:`MIND_ID_SOURCE_APP_STATE`.
    2. Live :func:`get_active_mind_id` lookup against the registry on
       ``request.app.state.registry``. Catches the multi-mind case
       where the cache may be stale. Source string:
       :data:`MIND_ID_SOURCE_MIND_MANAGER`.
    3. The literal sentinel ``"default"``. Source string:
       :data:`MIND_ID_SOURCE_FALLBACK_DEFAULT`.

    Returns ``(mind_id, source)`` so callers can emit structured
    telemetry citing where the resolution landed. Never raises —
    every step is wrapped in best-effort lookups (anti-pattern #33).

    Forensic anchor for the bug this resolves: the dashboard
    ``/api/voice/enable`` route at ``dashboard/routes/voice.py:1802``
    used to call ``getattr(request.app.state, "mind_id", "default")``
    while ``app.state.mind_id`` was never assigned anywhere in
    production code — the pipeline always operated under the phantom
    ``"default"`` mind even when the operator had created a real one.
    See ``c:\\Users\\guipe\\Downloads\\logs_01.txt`` line 1342 (every
    ``voice_pipeline_heartbeat`` carries ``mind_id=default`` despite
    the user's mind being ``jonny``).
    """
    cached = getattr(request.app.state, "mind_id", "")
    if isinstance(cached, str) and cached and cached != "default":
        return cached, MIND_ID_SOURCE_APP_STATE

    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            resolved = await get_active_mind_id(registry)
        except Exception:  # noqa: BLE001
            logger.debug("resolve_active_mind_id_for_request_failed")
        else:
            if resolved and resolved != "default":
                return resolved, MIND_ID_SOURCE_MIND_MANAGER

    # Last resort — preserves pre-T1.2 behaviour for fresh installs
    # where no mind has been initialised yet (genuine empty state).
    if isinstance(cached, str) and cached:
        # ``app.state.mind_id == "default"`` literal sentinel cached
        # by the server fallback — surface as app_state so dashboards
        # see "this came from the cache, not from a live registry
        # lookup that returned default".
        return cached, MIND_ID_SOURCE_APP_STATE
    # Phase 6.T6.3 (v0.40.1) — WARN before returning the literal
    # sentinel so any production occurrence is grep-able. Phase 1.T1.5
    # makes this path unreachable in a healthy daemon; ``get_active_mind_id``
    # docstring covers the test-fixture + transient-bootstrap rationale
    # for keeping the fallback.
    logger.warning(
        "dashboard.shared.fallback_default_mind",
        callsite="resolve_active_mind_id_for_request",
        reason=(
            "Neither app.state.mind_id nor MindManager produced a real "
            "mind id; falling back to the literal 'default' sentinel. "
            "See get_active_mind_id docstring for the closure rationale."
        ),
    )
    return "default", MIND_ID_SOURCE_FALLBACK_DEFAULT


async def resolve_mind_yaml_path_for_request(
    request: Request,
    *,
    explicit_mind_id: str | None = None,
) -> tuple[str, Path | None, str]:
    """Resolve ``(mind_id, mind_yaml_path, mind_id_source)`` for a request.

    Closes anti-pattern #35 reincidence #6 cluster Layer B
    (``MISSION-voice-zero-defect-2026-05-08.md`` Phase 3.A). Pre-fix
    ``server.py:775`` set ``app.state.mind_yaml_path`` ONCE at boot to
    ``data_dir / "aria" / "mind.yaml"`` regardless of which mind the
    operator was actively using; multi-mind operators had voice / config /
    onboarding / setup / providers persistence written to the phantom
    ``"aria"`` mind. This per-request resolver routes each persistence
    operation to the active mind's YAML.

    Mind id resolution:
        - If ``explicit_mind_id`` is supplied (non-empty, non-``"default"``
          sentinel), it is honoured as authoritative; source string is
          :data:`MIND_ID_SOURCE_EXPLICIT_REQUEST`. Used by routes that
          accept ``mind_id`` directly in the request body (e.g. wake-word
          training, calibration apply).
        - Otherwise falls back to :func:`resolve_active_mind_id_for_request`
          (cached app_state → live MindManager → ``"default"``).

    YAML path resolution order:
        1. ``request.app.state.mind_yaml_path`` if explicitly set —
           **test/legacy override**. Production code MUST NOT set this
           (the boot wire was removed in Phase 3.A); tests may still set
           it directly via ``application.state.mind_yaml_path = path``
           for dependency injection.
        2. ``data_dir / mind_id / "mind.yaml"`` where ``data_dir`` is read
           from :class:`EngineConfig.database.data_dir` via the registry.
           Returned only if the parent directory exists (preserving fresh-
           install + mind-not-initialised behaviour where persistence
           silently no-ops).
        3. ``None`` if neither path resolves. Callers MUST treat ``None``
           as "skip persistence" (matches pre-Phase-3.A semantics).

    Returns:
        A ``(mind_id, mind_yaml_path, source)`` triple. Never raises —
        every step is wrapped in best-effort lookups (anti-pattern #33).
    """
    from pathlib import Path

    # 1. Resolve mind id (the path depends on it).
    if explicit_mind_id and isinstance(explicit_mind_id, str) and explicit_mind_id != "default":
        mind_id, source = explicit_mind_id, MIND_ID_SOURCE_EXPLICIT_REQUEST
    else:
        mind_id, source = await resolve_active_mind_id_for_request(request)

    # 2. Test/legacy override: if a path was explicitly set on app.state,
    # honour it. The pre-Phase-3.A boot wire used to populate this in
    # production; the wire is removed (server.py no longer sets
    # ``app.state.mind_yaml_path``) so production code falls through to
    # step 3. Tests still rely on direct assignment for dependency
    # injection (``application.state.mind_yaml_path = test_path``).
    explicit_path = getattr(request.app.state, "mind_yaml_path", None)
    if explicit_path is not None:
        return mind_id, Path(explicit_path), source

    # 3. Production path: derive from EngineConfig.database.data_dir.
    registry = getattr(request.app.state, "registry", None)
    if registry is None:
        return mind_id, None, source

    try:
        from sovyx.engine.config import EngineConfig

        if not registry.is_registered(EngineConfig):
            return mind_id, None, source
        eng_cfg = await registry.resolve(EngineConfig)
        data_dir = eng_cfg.database.data_dir
    except Exception:  # noqa: BLE001 — defensive per anti-pattern #33
        logger.debug("resolve_mind_yaml_path_for_request_engine_config_failed")
        return mind_id, None, source

    # Defensive type check (anti-pattern #33): tests sometimes register a
    # plain ``MagicMock()`` (no ``spec=EngineConfig``) under is_registered
    # ``return_value=True``; the resolved object's ``.database.data_dir``
    # is then itself a MagicMock and not a usable ``Path``. Bail safely
    # so consumers fall through to the ``mind_yaml_path is None`` skip
    # branch instead of writing YAML to a phantom MagicMock path.
    if not isinstance(data_dir, Path):
        return mind_id, None, source

    yaml_path = data_dir / mind_id / "mind.yaml"
    # Preserve fresh-install semantics: if the mind directory doesn't
    # exist yet (no mind initialised), return None so callers skip
    # persistence rather than creating a phantom file in a non-existent
    # parent. ``MindManager`` creates the directory on mind creation.
    if not yaml_path.parent.exists():
        return mind_id, None, source
    return mind_id, yaml_path, source
