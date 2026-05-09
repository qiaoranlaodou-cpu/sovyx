"""Single source of truth for LLM model pricing.

One table used by every provider (`llm/providers/{anthropic,openai,google}.py`)
and the router (`llm/router.py`). Values are **USD per 1 million tokens**,
in `(input, output)` order.

Updating a model's rate means editing exactly one line below — the old
pattern of duplicating the same numbers in four files was responsible for
silent drift (e.g., `router.py` was missing `gemini-2.5-flash-preview-04-17`
while `providers/google.py` had it).

Last validated: 2026-04-16
Sources:
    - https://platform.claude.com/docs/en/docs/about-claude/pricing
    - https://openai.com/api/pricing/
    - https://ai.google.dev/pricing
    - https://docs.x.ai/docs/models
    - https://api-docs.deepseek.com/quick_start/pricing
    - https://docs.mistral.ai/getting-started/pricing/
    - https://www.together.ai/pricing
    - https://groq.com/pricing/
    - https://fireworks.ai/pricing
"""

from __future__ import annotations

from enum import StrEnum


class PricingSource(StrEnum):
    """Origin of a pricing tuple returned by :func:`get_pricing`.

    - ``EXACT`` — the model is in :data:`PRICING` and rates are authoritative.
    - ``PROVIDER_DEFAULT`` — the model is unknown but the provider is in
      :data:`PROVIDER_DEFAULT_PRICING`; rates are an estimate at the provider's
      typical cost band.
    - ``GLOBAL_DEFAULT`` — neither model nor provider matched; rates fall back
      to :data:`DEFAULT_PRICING` (Sonnet-class). Cost reports may be wildly
      off for cheap or expensive providers.
    """

    EXACT = "exact"
    PROVIDER_DEFAULT = "provider_default"
    GLOBAL_DEFAULT = "global_default"


# ── Per-model pricing (USD per 1M tokens) ──────────────────────────────
#
# Keep sorted within each provider block.

PRICING: dict[str, tuple[float, float]] = {
    # ── Anthropic (validated 2026-04-16) ──
    "claude-opus-4-20250514": (15.0, 75.0),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-5-20250514": (3.0, 15.0),
    "claude-sonnet-4-6-20250827": (3.0, 15.0),
    "claude-opus-4-5-20250918": (5.0, 25.0),
    "claude-opus-4-6-20250918": (5.0, 25.0),
    "claude-opus-4-7-20260401": (5.0, 25.0),
    # ── OpenAI (validated 2026-04-16) ──
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o1": (15.0, 60.0),
    "o3": (2.0, 8.0),
    "o3-mini": (1.1, 4.4),
    "o4-mini": (1.1, 4.4),
    # ── Google (validated 2026-04-16) ──
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-pro-preview-03-25": (1.25, 10.0),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-preview-04-17": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    # ── xAI / Grok (validated 2026-04-16) ──
    "grok-4": (2.0, 6.0),
    "grok-4.20-0309": (2.0, 6.0),
    "grok-4-1-fast": (0.20, 0.50),
    "grok-3": (3.0, 15.0),
    "grok-2": (2.0, 10.0),
    # ── DeepSeek (validated 2026-04-16, V3.2 unified pricing) ──
    "deepseek-chat": (0.28, 0.42),
    "deepseek-reasoner": (0.28, 0.42),
    # ── Mistral ──
    "mistral-large-latest": (2.0, 6.0),
    "mistral-small-latest": (0.10, 0.30),
    # ── Together AI ──
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.88, 0.88),
    "meta-llama/Llama-3.1-70B-Instruct-Turbo": (0.88, 0.88),
    "meta-llama/Llama-3.1-8B-Instruct-Turbo": (0.18, 0.18),
    # ── Groq (validated 2026-04-16) ──
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-4-scout-17b-16e-instruct": (0.11, 0.34),
    "qwen-3-32b": (0.29, 0.59),
    # ── Fireworks (parameter-tier pricing) ──
    "accounts/fireworks/models/llama-v3p3-70b-instruct": (0.90, 0.90),
    "accounts/fireworks/models/llama-v3p1-70b-instruct": (0.90, 0.90),
    "accounts/fireworks/models/llama-v3p1-8b-instruct": (0.20, 0.20),
    # ── Legacy (kept for backward compat, may be removed) ──
    "claude-3-5-haiku-20241022": (0.80, 4.0),
    "llama-3.1-70b-versatile": (0.59, 0.79),
    "mixtral-8x7b-32768": (0.24, 0.24),
}

# ── Per-model cache pricing (USD per 1M tokens) ───────────────────────
#
# Anthropic prompt-caching: cache reads cost 10% of input rate; the
# default 5-min cache write costs 125% of input rate (1h cache write
# costs 200% of input but isn't currently used by Sovyx). OpenAI's
# automatic prompt caching (gpt-4o family + o1/o3) reports
# ``prompt_tokens_details.cached_tokens`` which costs 50% of input
# (or 25% on some o-series models — verify against the table).
# Pair shape: ``(cache_read_per_1m, cache_creation_per_1m)``.
# Models absent from this table fall back to the input rate (no
# discount applied) — ``compute_cost_with_cache`` enforces this default.
#
# Last validated 2026-04-16 against the same provider docs as PRICING.

CACHE_PRICING: dict[str, tuple[float, float]] = {
    # ── Anthropic (cache_read = 0.1x input, cache_write_5m = 1.25x input) ──
    "claude-opus-4-20250514": (1.50, 18.75),
    "claude-sonnet-4-20250514": (0.30, 3.75),
    "claude-haiku-4-5-20251001": (0.10, 1.25),
    "claude-sonnet-4-5-20250514": (0.30, 3.75),
    "claude-sonnet-4-6-20250827": (0.30, 3.75),
    "claude-opus-4-5-20250918": (0.50, 6.25),
    "claude-opus-4-6-20250918": (0.50, 6.25),
    "claude-opus-4-7-20260401": (0.50, 6.25),
    "claude-3-5-haiku-20241022": (0.08, 1.00),
    # ── OpenAI (cache_read on prompt_tokens_details.cached_tokens) ──
    # OpenAI doesn't separately bill cache writes — the discount only
    # applies on reads. We map cache_read = 0.5x input for gpt-4o /
    # gpt-4.1 families, and 0.25x for o-series. cache_creation = input
    # rate (no surcharge — fresh prompts cost normal input).
    "gpt-4o": (1.25, 2.5),
    "gpt-4o-mini": (0.075, 0.15),
    "gpt-4.1": (0.50, 2.0),
    "gpt-4.1-mini": (0.10, 0.40),
    "gpt-4.1-nano": (0.025, 0.10),
    "o1": (7.50, 15.0),
    "o3": (0.50, 2.0),
    "o3-mini": (0.55, 1.1),
    "o4-mini": (0.275, 1.1),
}


# Conservative fallback (Sonnet-class) when the model is unknown and the
# caller hasn't supplied a provider-specific default.
DEFAULT_PRICING: tuple[float, float] = (3.0, 15.0)

# Per-provider fallbacks preserve the old per-file defaults so a missing
# model doesn't cross-contaminate cost estimates between providers.
PROVIDER_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "anthropic": (3.0, 15.0),
    "openai": (2.5, 10.0),
    "google": (0.30, 2.50),
    "ollama": (0.0, 0.0),  # local inference — free
    "xai": (2.0, 6.0),
    "deepseek": (0.28, 0.42),
    "mistral": (2.0, 6.0),
    "together": (0.88, 0.88),
    "groq": (0.59, 0.79),
    "fireworks": (0.90, 0.90),
}


def resolve_pricing_source(
    model: str | None,
    *,
    provider: str | None = None,
) -> PricingSource:
    """Classify which pricing tier :func:`get_pricing` would resolve.

    Mirrors the lookup logic without computing a cost — callers use this
    to decide whether to surface a fallback warning to the operator.

    Args:
        model: Model identifier, or ``None`` if unknown.
        provider: Provider name (e.g. ``"anthropic"``). If supplied and the
            model is unknown but the provider is in
            :data:`PROVIDER_DEFAULT_PRICING`, the source is
            :attr:`PricingSource.PROVIDER_DEFAULT`.

    Returns:
        :class:`PricingSource` describing which table answered the lookup.
    """
    if model is not None and model in PRICING:
        return PricingSource.EXACT
    if provider and provider in PROVIDER_DEFAULT_PRICING:
        return PricingSource.PROVIDER_DEFAULT
    return PricingSource.GLOBAL_DEFAULT


def get_pricing(
    model: str | None,
    *,
    fallback: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Return ``(input_per_1m, output_per_1m)`` pricing in USD.

    Args:
        model: The model identifier to look up.
        fallback: Price to use when the model is not in the table. Callers
            with a provider context should pass ``PROVIDER_DEFAULT_PRICING[name]``
            so an unknown model doesn't silently cost-estimate at another
            provider's rate.

    Returns:
        ``(input, output)`` rate per 1M tokens.
    """
    if model is not None and model in PRICING:
        return PRICING[model]
    return fallback if fallback is not None else DEFAULT_PRICING


def get_pricing_with_source(
    model: str | None,
    *,
    fallback: tuple[float, float] | None = None,
    provider: str | None = None,
) -> tuple[float, float, PricingSource]:
    """Return ``(input, output, source)`` for *model* in one call.

    Convenience wrapper for callers that need both the rates and the
    classification (e.g. dashboard endpoints that surface a warning when
    *source* is not :attr:`PricingSource.EXACT`).
    """
    source = resolve_pricing_source(model, provider=provider)
    price_in, price_out = get_pricing(model, fallback=fallback)
    return price_in, price_out, source


def compute_cost(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    fallback: tuple[float, float] | None = None,
) -> float:
    """Estimate the USD cost of a single call given its token counts.

    Treats every input token at the model's full input rate. Use
    :func:`compute_cost_with_cache` instead when the provider reports
    cached vs. fresh input separately — the cache discount is
    significant (90% off for Anthropic, 50% off for OpenAI) and
    swallowing it inflates billing reports.

    Args:
        model: Model identifier, or ``None`` if unknown.
        tokens_in: Input tokens consumed.
        tokens_out: Output tokens produced.
        fallback: See :func:`get_pricing`.

    Returns:
        Estimated cost in USD.
    """
    price_in, price_out = get_pricing(model, fallback=fallback)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def compute_cost_with_cache(
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    fallback: tuple[float, float] | None = None,
) -> float:
    """Estimate the USD cost of a single call accounting for cache discounts.

    *tokens_in* should be the count of FRESH input tokens — cached reads
    and cache creation should be passed via *cache_read_tokens* and
    *cache_creation_tokens* respectively. (Anthropic returns these on
    ``usage.cache_read_input_tokens`` / ``usage.cache_creation_input_tokens``;
    OpenAI returns the read count via ``usage.prompt_tokens_details.cached_tokens``.)

    Models absent from :data:`CACHE_PRICING` fall back to the regular
    input rate for both cache classes — the discount is silently lost
    rather than being mis-estimated.

    Args:
        model: Model identifier, or ``None`` if unknown.
        tokens_in: Fresh input tokens (NOT including cached reads).
        tokens_out: Output tokens produced.
        cache_read_tokens: Tokens served from a previous cache write.
        cache_creation_tokens: Tokens that wrote a new cache entry.
        fallback: See :func:`get_pricing`.

    Returns:
        Estimated cost in USD.
    """
    price_in, price_out = get_pricing(model, fallback=fallback)
    base = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    if cache_read_tokens == 0 and cache_creation_tokens == 0:
        return base

    if model is not None and model in CACHE_PRICING:
        price_cache_read, price_cache_create = CACHE_PRICING[model]
    else:
        price_cache_read = price_in
        price_cache_create = price_in

    cache = (
        cache_read_tokens * price_cache_read + cache_creation_tokens * price_cache_create
    ) / 1_000_000
    return base + cache
