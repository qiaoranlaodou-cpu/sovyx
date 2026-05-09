"""Tests for the unified LLM pricing module (sovyx.llm.pricing).

Every provider + the router delegates cost computation to this module,
so it's the single place the pricing table can drift — the tests below
pin the public contract.
"""

from __future__ import annotations

import pytest

from sovyx.llm.pricing import (
    CACHE_PRICING,
    DEFAULT_PRICING,
    PRICING,
    PROVIDER_DEFAULT_PRICING,
    PricingSource,
    compute_cost,
    compute_cost_with_cache,
    get_pricing,
    get_pricing_with_source,
    resolve_pricing_source,
)


class TestPricingTable:
    def test_every_provider_default_is_a_pair(self) -> None:
        for provider, pair in PROVIDER_DEFAULT_PRICING.items():
            assert len(pair) == 2, provider
            assert all(isinstance(v, (int, float)) for v in pair), provider

    def test_pricing_values_are_positive_or_zero(self) -> None:
        for model, (price_in, price_out) in PRICING.items():
            assert price_in >= 0, f"{model} input pricing negative"
            assert price_out >= 0, f"{model} output pricing negative"

    def test_default_pricing_is_sonnet_class(self) -> None:
        # DEFAULT_PRICING is the "conservative fallback" — Sonnet rates.
        assert DEFAULT_PRICING == (3.0, 15.0)


class TestGetPricing:
    def test_known_model_returns_exact_rate(self) -> None:
        assert get_pricing("gpt-4o") == PRICING["gpt-4o"]
        assert get_pricing("claude-sonnet-4-20250514") == PRICING["claude-sonnet-4-20250514"]

    def test_unknown_model_returns_default(self) -> None:
        assert get_pricing("vaporware-model-9999") == DEFAULT_PRICING

    def test_none_returns_default(self) -> None:
        assert get_pricing(None) == DEFAULT_PRICING

    def test_fallback_overrides_default(self) -> None:
        custom = (0.0, 0.0)
        assert get_pricing("vaporware", fallback=custom) == custom

    def test_fallback_ignored_for_known_model(self) -> None:
        # Fallback only kicks in on miss.
        custom = (999.0, 999.0)
        assert get_pricing("gpt-4o", fallback=custom) == PRICING["gpt-4o"]

    @pytest.mark.parametrize(
        "provider",
        [
            "anthropic",
            "openai",
            "google",
            "ollama",
            "xai",
            "deepseek",
            "mistral",
            "groq",
            "together",
            "fireworks",
        ],
    )
    def test_every_provider_has_a_default(self, provider: str) -> None:
        assert provider in PROVIDER_DEFAULT_PRICING


class TestComputeCost:
    def test_cost_matches_manual_calculation(self) -> None:
        tokens_in = 1_000_000
        tokens_out = 500_000
        price_in, price_out = PRICING["gpt-4o-mini"]
        expected = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
        assert compute_cost("gpt-4o-mini", tokens_in, tokens_out) == expected

    def test_zero_tokens_zero_cost(self) -> None:
        assert compute_cost("gpt-4o", 0, 0) == 0.0

    def test_ollama_provider_default_is_free(self) -> None:
        # Unknown-to-table model routed through the ollama fallback
        # should land on (0.0, 0.0) — local inference is free.
        fallback = PROVIDER_DEFAULT_PRICING["ollama"]
        assert compute_cost("llama3.1-8b", 10_000, 10_000, fallback=fallback) == 0.0

    def test_unknown_model_falls_back(self) -> None:
        # Without a provider-specific fallback, unknown models cost at
        # DEFAULT_PRICING — verify the math matches get_pricing.
        tokens_in, tokens_out = 1_000, 2_000
        expected = (tokens_in * DEFAULT_PRICING[0] + tokens_out * DEFAULT_PRICING[1]) / 1_000_000
        assert compute_cost("nope", tokens_in, tokens_out) == expected


class TestPricingBaseline:
    """Pin critical model prices to catch accidental drift."""

    _BASELINE: dict[str, tuple[float, float]] = {
        # Anthropic
        "claude-sonnet-4-20250514": (3.0, 15.0),
        "claude-haiku-4-5-20251001": (1.0, 5.0),
        "claude-opus-4-7-20260401": (5.0, 25.0),
        # OpenAI
        "gpt-4o": (2.5, 10.0),
        "gpt-4o-mini": (0.15, 0.6),
        "o3": (2.0, 8.0),
        "o3-mini": (1.1, 4.4),
        # Google
        "gemini-2.5-flash": (0.30, 2.50),
        "gemini-2.5-pro": (1.25, 10.0),
        # DeepSeek
        "deepseek-chat": (0.28, 0.42),
        "deepseek-reasoner": (0.28, 0.42),
        # xAI
        "grok-4": (2.0, 6.0),
        # Groq
        "llama-3.3-70b-versatile": (0.59, 0.79),
        "llama-3.1-8b-instant": (0.05, 0.08),
        # Together
        "meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.88, 0.88),
    }

    @pytest.mark.parametrize("model", list(_BASELINE.keys()))
    def test_price_matches_baseline(self, model: str) -> None:
        assert model in PRICING, f"{model} missing from PRICING table"
        assert PRICING[model] == self._BASELINE[model], (
            f"{model}: expected {self._BASELINE[model]}, got {PRICING[model]}"
        )

    def test_ollama_always_free(self) -> None:
        assert PROVIDER_DEFAULT_PRICING["ollama"] == (0.0, 0.0)

    def test_provider_defaults_updated(self) -> None:
        assert PROVIDER_DEFAULT_PRICING["openai"] == (2.5, 10.0)
        assert PROVIDER_DEFAULT_PRICING["deepseek"] == (0.28, 0.42)
        assert PROVIDER_DEFAULT_PRICING["xai"] == (2.0, 6.0)
        assert PROVIDER_DEFAULT_PRICING["google"] == (0.30, 2.50)


class TestPricingSource:
    """Issue #45 — fallback pricing source classification."""

    def test_known_model_is_exact(self) -> None:
        assert resolve_pricing_source("gpt-4o") is PricingSource.EXACT

    def test_known_model_with_provider_still_exact(self) -> None:
        # Provider hint doesn't downgrade an exact match.
        assert resolve_pricing_source("gpt-4o", provider="openai") is PricingSource.EXACT

    def test_unknown_model_with_known_provider_is_provider_default(self) -> None:
        assert (
            resolve_pricing_source("vaporware-7b", provider="anthropic")
            is PricingSource.PROVIDER_DEFAULT
        )

    def test_unknown_model_no_provider_is_global_default(self) -> None:
        assert resolve_pricing_source("vaporware-7b") is PricingSource.GLOBAL_DEFAULT

    def test_unknown_model_unknown_provider_is_global_default(self) -> None:
        assert (
            resolve_pricing_source("vaporware-7b", provider="totally-fake")
            is PricingSource.GLOBAL_DEFAULT
        )

    def test_none_model_is_global_default(self) -> None:
        assert resolve_pricing_source(None) is PricingSource.GLOBAL_DEFAULT


class TestGetPricingWithSource:
    def test_known_model_returns_exact_with_real_rate(self) -> None:
        price_in, price_out, source = get_pricing_with_source("gpt-4o")
        assert (price_in, price_out) == PRICING["gpt-4o"]
        assert source is PricingSource.EXACT

    def test_unknown_model_with_provider_uses_fallback(self) -> None:
        provider = "anthropic"
        provider_default = PROVIDER_DEFAULT_PRICING[provider]
        price_in, price_out, source = get_pricing_with_source(
            "vaporware",
            fallback=provider_default,
            provider=provider,
        )
        assert (price_in, price_out) == provider_default
        assert source is PricingSource.PROVIDER_DEFAULT

    def test_unknown_everything_is_global_default(self) -> None:
        price_in, price_out, source = get_pricing_with_source("vaporware")
        assert (price_in, price_out) == DEFAULT_PRICING
        assert source is PricingSource.GLOBAL_DEFAULT

    def test_pricing_source_enum_values(self) -> None:
        # StrEnum guarantees stable serialization across xdist
        # (anti-pattern #9 in CLAUDE.md).
        assert PricingSource.EXACT.value == "exact"
        assert PricingSource.PROVIDER_DEFAULT.value == "provider_default"
        assert PricingSource.GLOBAL_DEFAULT.value == "global_default"


class TestCachePricingTable:
    """Issue #44 — cache_read / cache_creation rates per model."""

    def test_every_cache_entry_is_a_pair(self) -> None:
        for model, pair in CACHE_PRICING.items():
            assert len(pair) == 2, model
            assert all(v >= 0 for v in pair), model

    def test_anthropic_cache_read_is_10pct_of_input(self) -> None:
        # Anthropic publishes cache reads at 0.1x input. Pin a few to
        # catch accidental regression of the rate table.
        for model in (
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-7-20260401",
        ):
            input_rate = PRICING[model][0]
            cache_read = CACHE_PRICING[model][0]
            assert cache_read == pytest.approx(input_rate * 0.1), (
                f"{model}: cache_read={cache_read} expected {input_rate * 0.1}"
            )

    def test_anthropic_cache_write_is_125pct_of_input(self) -> None:
        for model in (
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-7-20260401",
        ):
            input_rate = PRICING[model][0]
            cache_write = CACHE_PRICING[model][1]
            assert cache_write == pytest.approx(input_rate * 1.25), (
                f"{model}: cache_write={cache_write} expected {input_rate * 1.25}"
            )

    def test_openai_gpt4o_cache_read_is_50pct(self) -> None:
        # gpt-4o family bills cached reads at 50% of input.
        input_rate = PRICING["gpt-4o"][0]
        cache_read = CACHE_PRICING["gpt-4o"][0]
        assert cache_read == pytest.approx(input_rate * 0.5)


class TestComputeCostWithCache:
    """Issue #44 — discounted cost path."""

    def test_no_cache_tokens_matches_compute_cost(self) -> None:
        # Without any cache fields, the new function must agree with the
        # original compute_cost — backward-compat guarantee.
        for model in ("gpt-4o", "claude-sonnet-4-20250514"):
            assert compute_cost_with_cache(model, 1000, 500) == compute_cost(model, 1000, 500)

    def test_cache_read_uses_discounted_rate(self) -> None:
        # 10k cached tokens at the 0.1x rate cost 10x less than at full.
        model = "claude-sonnet-4-20250514"
        with_cache = compute_cost_with_cache(model, 0, 0, cache_read_tokens=10_000)
        as_full_input = compute_cost(model, 10_000, 0)
        assert with_cache == pytest.approx(as_full_input * 0.1)

    def test_cache_creation_uses_premium_rate(self) -> None:
        # Cache creation at 1.25x input is more expensive than fresh input.
        model = "claude-sonnet-4-20250514"
        creation = compute_cost_with_cache(model, 0, 0, cache_creation_tokens=10_000)
        as_full_input = compute_cost(model, 10_000, 0)
        assert creation == pytest.approx(as_full_input * 1.25)

    def test_unknown_model_falls_back_to_input_rate(self) -> None:
        # When the model isn't in CACHE_PRICING, both cache classes use
        # the input rate (no discount AND no surcharge applied).
        unknown = "vaporware-9000"
        cached = compute_cost_with_cache(
            unknown, 0, 0, cache_read_tokens=10_000, cache_creation_tokens=5_000
        )
        # Total = (10_000 + 5_000) at DEFAULT_PRICING input.
        expected = (15_000 * DEFAULT_PRICING[0]) / 1_000_000
        assert cached == pytest.approx(expected)

    def test_combined_fresh_plus_cache(self) -> None:
        model = "claude-sonnet-4-20250514"
        fresh_only = compute_cost_with_cache(model, 1_000, 500)
        with_cache = compute_cost_with_cache(model, 1_000, 500, cache_read_tokens=10_000)
        cache_in, _ = CACHE_PRICING[model]
        assert with_cache == pytest.approx(fresh_only + (10_000 * cache_in) / 1_000_000)

    def test_savings_from_cache_read(self) -> None:
        # Real-world scenario: 100k context with 90k cached, 10k fresh.
        # Cost should be ~19% of treating the whole 100k as fresh input.
        model = "claude-sonnet-4-20250514"
        cached = compute_cost_with_cache(model, 10_000, 0, cache_read_tokens=90_000)
        full_price = compute_cost(model, 100_000, 0)
        savings = 1 - (cached / full_price)
        # 90k @ 10% + 10k @ 100% = 19k effective tokens vs 100k naive.
        assert 0.79 < savings < 0.83, f"Cache savings = {savings:.2%}"
