"""Unit test — `BrainService.embedding_model_ready` (Mission C6 §T4.3).

The property is the fast readiness signal for the CognitiveLoop
dependency gate (anti-pattern #44). Reads ``self._embedding.has_embeddings``
defensively (handles partially-initialized fixtures).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sovyx.brain.service import BrainService


def _make_service(*, has_embeddings: bool | None) -> BrainService:
    """Construct a minimal BrainService whose embedding engine has the
    requested ``has_embeddings`` state. Other constructor deps are
    MagicMocks — they are unused by ``embedding_model_ready``.
    """
    svc = BrainService.__new__(BrainService)  # bypass __init__ for speed
    embedding = MagicMock()
    if has_embeddings is None:
        del embedding.has_embeddings  # exercise the missing-attribute branch
    else:
        embedding.has_embeddings = has_embeddings
    svc._embedding = embedding
    return svc


class TestEmbeddingModelReady:
    def test_returns_true_when_engine_has_embeddings(self) -> None:
        svc = _make_service(has_embeddings=True)
        assert svc.embedding_model_ready is True

    def test_returns_false_when_engine_lacks_embeddings(self) -> None:
        svc = _make_service(has_embeddings=False)
        assert svc.embedding_model_ready is False

    def test_returns_false_when_attribute_missing(self) -> None:
        """Defensive — pre-init engine state shouldn't crash the gate."""
        svc = _make_service(has_embeddings=None)
        assert svc.embedding_model_ready is False
