"""Sovyx LLM subsystem — providers, routing, cost control, discovery health.

Public surface (Mission C6 §T1.1, §T1.2):

* :class:`LLMProviderKey` — canonical 10-provider registry (single source of truth).
* :class:`DiscoveryVerdict` — eight-state classifier for router availability.
* :class:`LLMRouterDiscoveryReport` — immutable scanner report.
* :class:`ProviderHealthEntry` — per-provider health snapshot.
* :func:`scan_llm_provider_health` — pure-function discovery scanner.
"""

from sovyx.llm._provider_health import (
    DiscoveryVerdict,
    LLMRouterDiscoveryReport,
    ProviderHealthEntry,
    scan_llm_provider_health,
)
from sovyx.llm._provider_registry import LLMProviderKey

__all__ = [
    "DiscoveryVerdict",
    "LLMProviderKey",
    "LLMRouterDiscoveryReport",
    "ProviderHealthEntry",
    "scan_llm_provider_health",
]
