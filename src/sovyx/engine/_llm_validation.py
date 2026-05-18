"""Boot-time cloud-key validation (Mission C6 §T2.6).

Opt-in via ``tuning.llm.boot_key_validation_enabled`` (default False per
ADR-D10 — cloud probes cost real money on every cloud provider's API).
When enabled, runs a bounded-timeout transient probe against each
configured cloud provider at boot, populating the
``cloud_key_validation_results`` mapping that :func:`scan_llm_provider_
health` consumes to refine the ``CLOUD_KEY_INVALID`` verdict.

Without this module, the discovery scanner treats every key with
non-empty env-var as "available pending first call" — first-call
failure + the periodic liveness probe surface invalid keys without
paying the boot-time cost. Operators who want the verdict to reflect
real key validity at boot (e.g. fleet deployments where one bad key
should block the daemon from claiming "fully available") opt in.

Anti-pattern compliance:

* #14 — every probe is ``asyncio.wait_for``-bounded by
  ``tuning.llm.boot_key_validation_timeout_sec``; the bounded
  ``asyncio.gather(..., return_exceptions=True)`` walks the cloud
  providers concurrently so total boot-time overhead is roughly the
  ``timeout_sec`` (not ``N * timeout_sec``).
* #15 — bounded cardinality (9 cloud providers fixed by
  :class:`~sovyx.llm._provider_registry.LLMProviderKey`).
* #30 — no psutil iteration; ``asyncio.wait_for`` cancels probes that
  exceed the timeout via cooperative cancellation (no blocked syscalls).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from sovyx.cli._provider_setup_shared import create_provider, test_provider
from sovyx.llm._provider_registry import LLMProviderKey

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sovyx.engine.config import LLMTuningConfig


logger = logging.getLogger(__name__)


async def validate_cloud_keys_at_boot(
    env: Mapping[str, str],
    config: LLMTuningConfig,
) -> dict[str, bool]:
    """Run a bounded-timeout transient probe per configured cloud key.

    Returns ``{provider_value: True|False}`` for every cloud provider
    whose env-var is present + non-empty. Providers without an env-var
    set are NOT included in the result (they're "not configured" — the
    scanner returns ``key_valid=None`` for those).

    Idempotent + side-effect free: the transient provider instances are
    discarded; no router state is modified; no env-vars are mutated.

    The function NEVER raises — any per-provider exception is captured
    and recorded as ``False`` with a structured ``llm.boot_validation.
    probe_failed`` WARN. The boot path proceeds regardless.

    No-op when ``config.boot_key_validation_enabled`` is False — returns
    an empty dict so the caller knows validation did not run.
    """
    if not config.boot_key_validation_enabled:
        return {}

    cloud_members = [key for key in LLMProviderKey if key.is_cloud]
    candidates: list[tuple[LLMProviderKey, str]] = []
    for key in cloud_members:
        env_value = env.get(key.env_var, "")
        if env_value:
            candidates.append((key, env_value))

    if not candidates:
        logger.info(
            "llm.boot_validation.no_candidates",
            extra={"cloud_member_count": len(cloud_members)},
        )
        return {}

    logger.info(
        "llm.boot_validation.started",
        extra={
            "candidate_count": len(candidates),
            "timeout_sec_per_key": config.boot_key_validation_timeout_sec,
        },
    )

    results = await asyncio.gather(
        *[
            _probe_single_key(key, api_key, config.boot_key_validation_timeout_sec)
            for key, api_key in candidates
        ],
        return_exceptions=False,
    )
    validation_map = dict(results)
    valid_count = sum(1 for v in validation_map.values() if v)
    logger.info(
        "llm.boot_validation.completed",
        extra={
            "valid_count": valid_count,
            "invalid_count": len(validation_map) - valid_count,
            "providers_probed": list(validation_map.keys()),
        },
    )
    return validation_map


async def _probe_single_key(
    key: LLMProviderKey,
    api_key: str,
    timeout_sec: float,
) -> tuple[str, bool]:
    """Probe one cloud provider; bounded by ``timeout_sec``.

    Returns ``(provider_value, is_valid)``. Captures all exceptions —
    treats them as ``is_valid=False`` with a structured WARN log line.
    """
    try:
        instance = create_provider(key.value, api_key)
        if instance is None:
            logger.warning(
                "llm.boot_validation.create_failed",
                extra={"provider": key.value},
            )
            return key.value, False
        ok, message = await asyncio.wait_for(
            test_provider(instance),
            timeout=timeout_sec,
        )
        if not ok:
            logger.warning(
                "llm.boot_validation.probe_failed",
                extra={
                    "provider": key.value,
                    "reason": "test_provider_returned_false",
                    "message": message,
                },
            )
        return key.value, ok
    except TimeoutError:
        logger.warning(
            "llm.boot_validation.probe_failed",
            extra={
                "provider": key.value,
                "reason": "timeout",
                "timeout_sec": timeout_sec,
            },
        )
        return key.value, False
    except Exception as exc:  # noqa: BLE001 — observability-only surface
        logger.warning(
            "llm.boot_validation.probe_failed",
            extra={
                "provider": key.value,
                "reason": "exception",
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return key.value, False


def env_snapshot_for_validation() -> dict[str, str]:
    """Return a defensive snapshot of ``os.environ`` for validation.

    Returns a plain ``dict`` so the caller cannot accidentally mutate
    ``os.environ`` mid-validation (anti-pattern #23 sibling — keep
    process env stable during boot).
    """
    return dict(os.environ)


__all__ = ["env_snapshot_for_validation", "validate_cloud_keys_at_boot"]
