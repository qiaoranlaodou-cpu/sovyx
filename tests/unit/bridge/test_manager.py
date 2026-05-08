"""Tests for sovyx.bridge.manager — BridgeManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, PropertyMock

from sovyx.bridge.manager import BridgeManager
from sovyx.bridge.protocol import InboundMessage
from sovyx.cognitive.act import ActionResult
from sovyx.engine.errors import CognitiveError
from sovyx.engine.types import (
    ChannelType,
    ConversationId,
    MindId,
    PersonId,
)

MIND = MindId("aria")
PERSON = PersonId("person-1")
CONV = ConversationId("conv-1")


def _inbound(
    text: str = "Hello",
    user_id: str = "123",
    chat_id: str = "123",
) -> InboundMessage:
    return InboundMessage(
        channel_type=ChannelType.TELEGRAM,
        channel_user_id=user_id,
        channel_message_id="msg1",
        chat_id=chat_id,
        text=text,
        display_name="Guipe",
    )


def _mock_resolver(person_id: PersonId = PERSON) -> AsyncMock:
    r = AsyncMock()
    r.resolve = AsyncMock(return_value=person_id)
    return r


def _mock_tracker(
    history: list[dict[str, str]] | None = None,
) -> AsyncMock:
    t = AsyncMock()
    t.get_or_create = AsyncMock(return_value=(CONV, history or []))
    t.add_turn = AsyncMock()
    return t


def _mock_gate(
    result: ActionResult | None = None,
    error: Exception | None = None,
) -> AsyncMock:
    gate = AsyncMock()
    if error:
        gate.submit = AsyncMock(side_effect=error)
    else:
        gate.submit = AsyncMock(
            return_value=result or ActionResult(response_text="Hi!", target_channel="telegram")
        )
    return gate


def _mock_adapter() -> AsyncMock:
    adapter = AsyncMock()
    type(adapter).channel_type = PropertyMock(return_value=ChannelType.TELEGRAM)
    adapter.send = AsyncMock(return_value="sent1")
    adapter.start = AsyncMock()
    adapter.stop = AsyncMock()
    return adapter


def _manager(
    gate: AsyncMock | None = None,
    resolver: AsyncMock | None = None,
    tracker: AsyncMock | None = None,
) -> BridgeManager:
    return BridgeManager(
        event_bus=AsyncMock(),
        cog_loop_gate=gate or _mock_gate(),
        person_resolver=resolver or _mock_resolver(),
        conversation_tracker=tracker or _mock_tracker(),
        mind_id=MIND,
    )


class TestInboundPipeline:
    """Full inbound pipeline."""

    async def test_full_pipeline(self) -> None:
        gate = _mock_gate()
        tracker = _mock_tracker()
        adapter = _mock_adapter()
        mgr = _manager(gate=gate, tracker=tracker)
        mgr.register_channel(adapter)

        await mgr.handle_inbound(_inbound())

        gate.submit.assert_called_once()
        tracker.add_turn.assert_any_call(CONV, "user", "Hello")
        tracker.add_turn.assert_any_call(
            CONV,
            "assistant",
            "Hi!",
            metadata={"tags": ["brain"]},
        )
        adapter.send.assert_called_once()

    async def test_person_resolved(self) -> None:
        resolver = _mock_resolver()
        mgr = _manager(resolver=resolver)
        mgr.register_channel(_mock_adapter())

        await mgr.handle_inbound(_inbound())

        resolver.resolve.assert_called_once_with(ChannelType.TELEGRAM, "123", "Guipe")

    async def test_chat_id_as_target(self) -> None:
        """v9 fix: target = chat_id (not channel_user_id)."""
        adapter = _mock_adapter()
        mgr = _manager()
        mgr.register_channel(adapter)

        await mgr.handle_inbound(_inbound(chat_id="-999"))

        call_args = adapter.send.call_args
        assert call_args[0][0] == "-999"  # target is chat_id


class TestFiltering:
    """Filtered messages."""

    async def test_filtered_no_response(self) -> None:
        gate = _mock_gate(
            result=ActionResult(
                response_text="",
                target_channel="telegram",
                filtered=True,
            )
        )
        adapter = _mock_adapter()
        tracker = _mock_tracker()
        mgr = _manager(gate=gate, tracker=tracker)
        mgr.register_channel(adapter)

        await mgr.handle_inbound(_inbound())

        # User turn always recorded
        tracker.add_turn.assert_called_once_with(CONV, "user", "Hello")
        # No response sent
        adapter.send.assert_not_called()


class TestErrorHandling:
    """Error handling."""

    async def test_gate_timeout(self) -> None:
        gate = _mock_gate(error=CognitiveError("timeout"))
        adapter = _mock_adapter()
        mgr = _manager(gate=gate)
        mgr.register_channel(adapter)

        await mgr.handle_inbound(_inbound())

        # Fallback message sent
        adapter.send.assert_called_once()
        sent_text = adapter.send.call_args[0][1]
        assert "went wrong" in sent_text

    async def test_unexpected_error_sends_error_response(self) -> None:
        """When pipeline crashes unexpectedly, user still gets error message."""
        gate = _mock_gate()
        gate.submit = AsyncMock(side_effect=RuntimeError("unexpected DB failure"))
        adapter = _mock_adapter()
        mgr = _manager(gate=gate)
        mgr.register_channel(adapter)

        await mgr.handle_inbound(_inbound())

        # Error response sent to user (not silence)
        adapter.send.assert_called_once()
        sent_text = adapter.send.call_args[0][1]
        assert "went wrong" in sent_text.lower() or "try again" in sent_text.lower()

    async def test_send_failure_no_crash(self) -> None:
        adapter = _mock_adapter()
        adapter.send = AsyncMock(side_effect=RuntimeError("network error"))
        mgr = _manager()
        mgr.register_channel(adapter)

        # Should not raise
        await mgr.handle_inbound(_inbound())

    async def test_no_adapter_no_crash(self) -> None:
        mgr = _manager()
        # No adapter registered
        await mgr.handle_inbound(_inbound())


class TestRaceCondition:
    """v13 fix: per-conversation lock."""

    async def test_same_user_serialized(self) -> None:
        """Two messages from same user: second sees first's history."""
        call_order: list[str] = []
        tracker = _mock_tracker()

        async def track_add_turn(
            conv_id: ConversationId,
            role: str,
            content: str,
            **kwargs: object,
        ) -> None:
            call_order.append(f"{role}:{content}")
            await asyncio.sleep(0.01)

        tracker.add_turn = AsyncMock(side_effect=track_add_turn)

        gate = _mock_gate()
        adapter = _mock_adapter()
        mgr = _manager(gate=gate, tracker=tracker)
        mgr.register_channel(adapter)

        msg_a = _inbound(text="first")
        msg_b = _inbound(text="second")

        await asyncio.gather(
            mgr.handle_inbound(msg_a),
            mgr.handle_inbound(msg_b),
        )

        # Both processed
        assert len(call_order) == 4  # noqa: PLR2004
        # Per-conversation lock ensures serialization
        user_turns = [c for c in call_order if c.startswith("user:")]
        assert len(user_turns) == 2  # noqa: PLR2004

    async def test_different_users_parallel(self) -> None:
        """Different users have different conv_ids → no serialization."""
        tracker = AsyncMock()
        call_count = 0

        async def get_or_create(
            mind_id: MindId,
            person_id: PersonId,
            channel_type: ChannelType,
        ) -> tuple[ConversationId, list[dict[str, str]]]:
            nonlocal call_count
            call_count += 1
            # Different conv_id per person
            cid = ConversationId(f"conv-{person_id}")
            return cid, []

        tracker.get_or_create = AsyncMock(side_effect=get_or_create)
        tracker.add_turn = AsyncMock()

        resolver = AsyncMock()

        async def resolve(ct: ChannelType, uid: str, name: str) -> PersonId:
            return PersonId(f"person-{uid}")

        resolver.resolve = AsyncMock(side_effect=resolve)

        gate = _mock_gate()
        adapter = _mock_adapter()
        mgr = BridgeManager(AsyncMock(), gate, resolver, tracker, MIND)
        mgr.register_channel(adapter)

        msg_a = _inbound(text="A", user_id="100", chat_id="100")
        msg_b = _inbound(text="B", user_id="200", chat_id="200")

        await asyncio.gather(
            mgr.handle_inbound(msg_a),
            mgr.handle_inbound(msg_b),
        )

        # Both processed with different locks
        assert len(mgr._conv_locks) == 2  # noqa: PLR2004


class TestChannelManagement:
    """Channel registration and lifecycle."""

    def test_register_channel(self) -> None:
        mgr = _manager()
        adapter = _mock_adapter()
        mgr.register_channel(adapter)
        assert mgr._get_adapter(ChannelType.TELEGRAM) is adapter

    def test_unknown_channel_returns_none(self) -> None:
        mgr = _manager()
        assert mgr._get_adapter(ChannelType.CLI) is None

    async def test_start_starts_adapters(self) -> None:
        mgr = _manager()
        adapter = _mock_adapter()
        mgr.register_channel(adapter)
        await mgr.start()
        adapter.start.assert_called_once()

    async def test_stop_stops_adapters(self) -> None:
        mgr = _manager()
        adapter = _mock_adapter()
        mgr.register_channel(adapter)
        await mgr.stop()
        adapter.stop.assert_called_once()


class TestLRULockDict:
    """Bounded lock dictionary with LRU eviction."""

    def test_eviction_at_capacity(self) -> None:
        from sovyx.bridge.manager import _LRULockDict

        d: _LRULockDict[str] = _LRULockDict(maxsize=3)
        d.setdefault("a", asyncio.Lock())
        d.setdefault("b", asyncio.Lock())
        d.setdefault("c", asyncio.Lock())
        assert len(d) == 3

        # Adding 4th evicts oldest ("a")
        d.setdefault("d", asyncio.Lock())
        assert len(d) == 3
        # "a" was evicted, "d" is present
        lock_d = d.setdefault("d", asyncio.Lock())
        assert lock_d is not None

    def test_access_promotes_to_end(self) -> None:
        from sovyx.bridge.manager import _LRULockDict

        d: _LRULockDict[str] = _LRULockDict(maxsize=3)
        lock_a = d.setdefault("a", asyncio.Lock())
        d.setdefault("b", asyncio.Lock())
        d.setdefault("c", asyncio.Lock())

        # Access "a" to promote it
        d.setdefault("a", asyncio.Lock())
        # Now "b" is oldest. Adding "d" should evict "b", not "a"
        d.setdefault("d", asyncio.Lock())
        assert len(d) == 3
        # "a" should still be accessible
        result = d.setdefault("a", asyncio.Lock())
        assert result is lock_a

    async def test_setdefault_skips_locked_eviction_target(self) -> None:
        """v0.31.7 T3.3 (LOW.1): eviction MUST skip locked entries.

        Pre-T3.3 the eviction was unconditional ``popitem(last=False)``
        on the LRU entry — a held lock could be orphaned. Now eviction
        walks LRU→MRU and drops the first UNHELD entry, leaving held
        locks intact.
        """
        from sovyx.bridge.manager import _LRULockDict

        d: _LRULockDict[str] = _LRULockDict(maxsize=3)
        lock_a = d.setdefault("a", asyncio.Lock())
        d.setdefault("b", asyncio.Lock())
        d.setdefault("c", asyncio.Lock())

        # Acquire the LRU entry (`a`) — eviction should skip it.
        await lock_a.acquire()
        try:
            # Insert a 4th key. With "a" held, eviction picks "b"
            # (next-oldest UNHELD).
            d.setdefault("d", asyncio.Lock())
            # "a" stays in the dict because it was held.
            assert "a" in d
            assert "b" not in d  # the unheld one was evicted
            assert "c" in d
            assert "d" in d
            assert len(d) == 3
        finally:
            lock_a.release()


class TestBoundedConfirmationsDictT36:
    """v0.31.7 T3.6 (LOW.5): _pending_confirmations is bounded so an
    operator who abandons confirmations doesn't accumulate entries
    forever. LRU-by-insertion-order eviction.
    """

    def test_pending_confirmations_evicts_oldest_at_capacity(self) -> None:
        from sovyx.bridge.manager import (
            _BoundedConfirmationsDict,
            _PendingConfirmationCtx,
        )

        d = _BoundedConfirmationsDict(maxsize=3)

        def _make(chat_id: str) -> _PendingConfirmationCtx:
            return _PendingConfirmationCtx(
                message_id=f"msg-{chat_id}",
                chat_id=chat_id,
                channel_type=ChannelType.TELEGRAM,
                tool_call_ids=[],
                is_batch=False,
            )

        d["chat_a"] = _make("chat_a")
        d["chat_b"] = _make("chat_b")
        d["chat_c"] = _make("chat_c")
        assert len(d) == 3
        assert "chat_a" in d

        # 4th insert evicts the oldest (chat_a).
        d["chat_d"] = _make("chat_d")
        assert len(d) == 3
        assert "chat_a" not in d
        assert "chat_b" in d
        assert "chat_c" in d
        assert "chat_d" in d

    def test_pop_returns_value_and_default(self) -> None:
        from sovyx.bridge.manager import (
            _BoundedConfirmationsDict,
            _PendingConfirmationCtx,
        )

        d = _BoundedConfirmationsDict(maxsize=3)
        ctx = _PendingConfirmationCtx(
            message_id="msg-1",
            chat_id="chat_1",
            channel_type=ChannelType.TELEGRAM,
            tool_call_ids=[],
        )
        d["chat_1"] = ctx

        result = d.pop("chat_1", None)
        assert result is ctx
        assert "chat_1" not in d

        # second pop returns the default
        result_again = d.pop("chat_1", None)
        assert result_again is None

    def test_reinsert_updates_value_without_growing_size(self) -> None:
        from sovyx.bridge.manager import (
            _BoundedConfirmationsDict,
            _PendingConfirmationCtx,
        )

        d = _BoundedConfirmationsDict(maxsize=3)
        ctx_v1 = _PendingConfirmationCtx(
            message_id="msg-v1",
            chat_id="chat_1",
            channel_type=ChannelType.TELEGRAM,
            tool_call_ids=[],
        )
        ctx_v2 = _PendingConfirmationCtx(
            message_id="msg-v2",
            chat_id="chat_1",
            channel_type=ChannelType.TELEGRAM,
            tool_call_ids=[],
        )
        d["chat_1"] = ctx_v1
        d["chat_1"] = ctx_v2  # reinsert, same key

        assert len(d) == 1
        assert d["chat_1"].message_id == "msg-v2"
