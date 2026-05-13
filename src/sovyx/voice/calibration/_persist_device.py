"""Shared persistence helper for the operator's mic choice.

Writes the paired ``voice_input_device_name`` + ``voice_input_device_host_api``
fields to a per-mind ``mind.yaml`` via :class:`~sovyx.engine.config_editor.ConfigEditor`
(atomic, lock-per-path, comment-preserving). Single source of truth for
the dashboard voice-enable endpoint, the ``sovyx voice setup`` CLI
command, and any future entry point that needs to commit a mic choice
to the operator's mind config.

Contract:

* ``device_name`` MUST be a non-empty string after ``.strip()`` —
  empty / whitespace-only names raise :class:`ValueError`. Callers
  that have a sentinel "no preference" must not invoke this helper;
  the absence of a write IS the no-preference signal.
* ``host_api`` is optional. PortAudio enumerations on some platforms
  (older Windows MME stacks, certain JACK builds) surface devices
  without a host-API name; the calibration resolver
  (:mod:`sovyx.voice.calibration._active_mic`) tolerates a missing
  ``host_api`` so the helper does too.
* In-memory mirror — when ``mind_config`` is provided, the helper
  sets the matching attributes so the live config object stays in
  sync with disk without requiring a daemon restart. Best-effort:
  any AttributeError on the mirror is swallowed (matches the
  dashboard's :func:`contextlib.suppress` pattern at
  :code:`dashboard/routes/voice.py:2825-2830`).

History: introduced 2026-05-13 (Mission §Phase 2.T2.3) as a
DRY-and-prerequisite step before the new :command:`sovyx voice setup`
CLI command (T2.1). The dashboard's inline write at
:code:`routes/voice.py:2802-2813` is migrated to this helper in the
same change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sovyx.engine.config_editor import ConfigEditor
from sovyx.observability.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


async def persist_voice_input_device(
    *,
    mind_yaml_path: Path,
    device_name: str,
    host_api: str | None = None,
    mind_config: Any = None,  # noqa: ANN401 — duck-typed MindConfig | mock
) -> None:
    """Persist the operator's mic choice into a mind.yaml + mirror.

    Args:
        mind_yaml_path: Absolute path to the target ``mind.yaml``. The
            file MUST exist; the helper does not create new mind
            directories (the resolver / wizard chain owns that).
        device_name: Non-empty PortAudio device name as enumerated by
            :func:`sounddevice.query_devices`. Surrounding whitespace
            is stripped before write.
        host_api: PortAudio host-API name (e.g. ``"Windows WASAPI"``,
            ``"ALSA"``, ``"Core Audio"``). Optional — pass ``None``
            when the enumeration source did not expose it.
        mind_config: Optional in-memory :class:`~sovyx.mind.config.MindConfig`
            (or any duck-typed object exposing the matching attributes)
            that should be updated to match the freshly-persisted
            values. Each attribute set is wrapped in best-effort
            error-swallowing so a frozen / unrelated mock object does
            not break the disk-persist contract.

    Raises:
        ValueError: ``device_name`` is empty / whitespace-only.
        FileNotFoundError: ``mind_yaml_path`` does not exist.

    Side effects:
        * Writes ``voice_input_device_name`` (always) and, when
          ``host_api`` is provided + non-empty, ``voice_input_device_host_api``
          to ``mind_yaml_path`` via :class:`ConfigEditor.set_scalar`
          (atomic, lock-per-path).
        * Emits structured INFO ``voice.config.input_device_persisted``
          with the mind.yaml path + non-PII length-hashes of the values.
        * Mutates ``mind_config`` in place when provided (best-effort).
    """
    normalized_name = device_name.strip()
    if not normalized_name:
        msg = "device_name must be a non-empty string"
        raise ValueError(msg)
    normalized_host_api = host_api.strip() if host_api else ""

    resolved_path = mind_yaml_path.expanduser().resolve()
    if not resolved_path.is_file():
        msg = f"mind.yaml not found at {resolved_path}. Run `sovyx init` first to create the mind."
        raise FileNotFoundError(msg)

    editor = ConfigEditor()
    await editor.set_scalar(resolved_path, "voice_input_device_name", normalized_name)
    if normalized_host_api:
        await editor.set_scalar(
            resolved_path,
            "voice_input_device_host_api",
            normalized_host_api,
        )

    # In-memory mirror — best-effort, matches the dashboard's existing
    # ``contextlib.suppress`` pattern at routes/voice.py:2825-2830. A
    # frozen MindConfig or a test-time MagicMock with restricted attrs
    # must not break the disk-persist contract.
    if mind_config is not None:
        try:
            mind_config.voice_input_device_name = normalized_name
        except (AttributeError, TypeError) as exc:  # noqa: BLE001 — narrow + log
            logger.info(
                "voice.config.input_device_mirror_skipped",
                attribute="voice_input_device_name",
                reason=type(exc).__name__,
            )
        if normalized_host_api:
            try:
                mind_config.voice_input_device_host_api = normalized_host_api
            except (AttributeError, TypeError) as exc:  # noqa: BLE001
                logger.info(
                    "voice.config.input_device_mirror_skipped",
                    attribute="voice_input_device_host_api",
                    reason=type(exc).__name__,
                )

    logger.info(
        "voice.config.input_device_persisted",
        mind_yaml_path=str(resolved_path),
        device_name_len=len(normalized_name),
        host_api_present=bool(normalized_host_api),
    )
