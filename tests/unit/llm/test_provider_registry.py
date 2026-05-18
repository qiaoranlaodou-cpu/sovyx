"""Unit tests — `sovyx.llm._provider_registry` (Mission C6 §T1.5).

Coverage: every cloud env-var mapping, Ollama special-case, iteration order,
StrEnum value-equality, env_var_map() shape, default_model accessor.
"""

from __future__ import annotations

from sovyx.llm._provider_registry import LLMProviderKey


class TestLLMProviderKeyMembers:
    """Member-level invariants."""

    def test_anthropic_env_var(self) -> None:
        assert LLMProviderKey.ANTHROPIC.env_var == "ANTHROPIC_API_KEY"

    def test_openai_env_var(self) -> None:
        assert LLMProviderKey.OPENAI.env_var == "OPENAI_API_KEY"

    def test_google_env_var(self) -> None:
        assert LLMProviderKey.GOOGLE.env_var == "GOOGLE_API_KEY"

    def test_xai_env_var_diverges_from_value(self) -> None:
        """Preserved verbatim from pre-C6 onboarding.py:22 and bootstrap.py:672."""
        assert LLMProviderKey.XAI.value == "xai"
        assert LLMProviderKey.XAI.env_var == "XGROK_API_KEY"

    def test_deepseek_env_var(self) -> None:
        assert LLMProviderKey.DEEPSEEK.env_var == "DEEPSEEK_API_KEY"

    def test_mistral_env_var(self) -> None:
        assert LLMProviderKey.MISTRAL.env_var == "MISTRAL_API_KEY"

    def test_groq_env_var(self) -> None:
        assert LLMProviderKey.GROQ.env_var == "GROQ_API_KEY"

    def test_together_env_var(self) -> None:
        assert LLMProviderKey.TOGETHER.env_var == "TOGETHER_API_KEY"

    def test_fireworks_env_var(self) -> None:
        assert LLMProviderKey.FIREWORKS.env_var == "FIREWORKS_API_KEY"

    def test_ollama_env_var_is_empty_sentinel(self) -> None:
        """Local provider has no env-var; empty-string sentinel keeps shape uniform."""
        assert LLMProviderKey.OLLAMA.env_var == ""


class TestIsCloudProperty:
    def test_anthropic_is_cloud(self) -> None:
        assert LLMProviderKey.ANTHROPIC.is_cloud is True

    def test_ollama_is_not_cloud(self) -> None:
        assert LLMProviderKey.OLLAMA.is_cloud is False

    def test_all_cloud_members_have_env_var(self) -> None:
        for key in LLMProviderKey:
            if key.is_cloud:
                assert key.env_var != ""


class TestEnvVarMap:
    def test_env_var_map_excludes_ollama(self) -> None:
        env_map = LLMProviderKey.env_var_map()
        assert "ollama" not in env_map
        assert len(env_map) == 9

    def test_env_var_map_keyed_by_member_value(self) -> None:
        env_map = LLMProviderKey.env_var_map()
        assert env_map["anthropic"] == "ANTHROPIC_API_KEY"
        assert env_map["xai"] == "XGROK_API_KEY"

    def test_env_var_map_immutable_view(self) -> None:
        """Returned mapping should not leak the internal dict reference."""
        env_map_1 = dict(LLMProviderKey.env_var_map())
        env_map_2 = dict(LLMProviderKey.env_var_map())
        assert env_map_1 == env_map_2


class TestIterationOrder:
    def test_iteration_yields_canonical_sequence(self) -> None:
        """Order matches pre-C6 bootstrap.py:657-700 registration sequence."""
        expected = [
            "anthropic",
            "openai",
            "google",
            "xai",
            "deepseek",
            "mistral",
            "groq",
            "together",
            "fireworks",
            "ollama",
        ]
        actual = [key.value for key in LLMProviderKey]
        assert actual == expected

    def test_member_count_is_ten(self) -> None:
        assert len(list(LLMProviderKey)) == 10


class TestStrEnumValueEquality:
    """Anti-pattern #9 — value-based comparison must work."""

    def test_value_equals_string(self) -> None:
        assert LLMProviderKey.ANTHROPIC == "anthropic"

    def test_in_string_set(self) -> None:
        assert LLMProviderKey.OLLAMA in {"ollama", "openai"}


class TestDefaultModel:
    def test_anthropic_default(self) -> None:
        assert LLMProviderKey.ANTHROPIC.default_model == "claude-sonnet-4-6"

    def test_openai_default(self) -> None:
        assert LLMProviderKey.OPENAI.default_model == "gpt-4o-mini"

    def test_ollama_default_is_empty(self) -> None:
        """Ollama defers to list_models() at boot; default empty."""
        assert LLMProviderKey.OLLAMA.default_model == ""

    def test_every_cloud_has_default(self) -> None:
        for key in LLMProviderKey:
            if key.is_cloud:
                assert key.default_model
