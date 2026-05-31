"""Tests for Sovyx Plugin Sandbox HTTP — domain allowlist, rate limiting.

Coverage target: ≥95% on plugins/sandbox_http.py
"""

from __future__ import annotations

import gzip
from collections.abc import AsyncIterator, Callable
from unittest.mock import patch

import httpx
import pytest

from sovyx.plugins import sandbox_http as _sbx_mod  # anti-pattern #11
from sovyx.plugins.permissions import PermissionDeniedError
from sovyx.plugins.sandbox_http import (
    SandboxedHttpClient,
    _is_local_ip,
    _RateLimiter,
    _resolve_hostname,
)

# ── MockTransport harness (gold standard) ───────────────────────────
#
# The production client now STREAMS: it does
# ``build_request(...)`` + ``send(req, stream=True)``, walks redirects
# calling ``aclose()`` on each unread 3xx body, then reads the final
# body incrementally via ``aiter_bytes()`` under a running size cap and
# rebuilds a buffered ``httpx.Response``. ``patch.object(client._client,
# "request", ...)`` therefore intercepts NOTHING (the prod code never
# calls ``.request``). We instead swap in an ``httpx.AsyncClient`` backed
# by ``httpx.MockTransport`` so the REAL send / stream / redirect path is
# exercised end-to-end, with ``follow_redirects=False`` preserved (the
# SSRF invariant — the prod redirect walk must stay manual).

Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client(handler: Handler) -> httpx.AsyncClient:
    """Build a MockTransport-backed client preserving follow_redirects=False.

    ``follow_redirects=False`` is CRITICAL: the production SSRF closure
    depends on httpx NOT auto-following 3xx, so the sandbox can validate
    every hop. A MockTransport client that auto-follows would mask that.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )


class _CountingStream(httpx.AsyncByteStream):
    """An async byte stream that records exactly how many bytes were pulled.

    Used to PROVE the streaming size cap aborts the read early (it stops
    iterating before the whole body is produced) and that redirect bodies
    are never pulled at all. ``pulled_bytes`` / ``pulled_chunks`` reflect
    only what the consumer actually requested.
    """

    def __init__(self, chunk: bytes, count: int) -> None:
        self._chunk = chunk
        self._count = count
        self.pulled_bytes = 0
        self.pulled_chunks = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._count):
            self.pulled_bytes += len(self._chunk)
            self.pulled_chunks += 1
            yield self._chunk

    async def aclose(self) -> None:  # pragma: no cover - nothing to release
        return None


# ── Local IP Detection ──────────────────────────────────────────────


class TestIsLocalIp:
    """Tests for _is_local_ip."""

    def test_loopback_v4(self) -> None:
        assert _is_local_ip("127.0.0.1") is True

    def test_loopback_v6(self) -> None:
        assert _is_local_ip("::1") is True

    def test_private_10(self) -> None:
        assert _is_local_ip("10.0.0.1") is True

    def test_private_172(self) -> None:
        assert _is_local_ip("172.16.0.1") is True

    def test_private_192(self) -> None:
        assert _is_local_ip("192.168.1.1") is True

    def test_link_local(self) -> None:
        assert _is_local_ip("169.254.1.1") is True

    def test_public_ip(self) -> None:
        assert _is_local_ip("8.8.8.8") is False

    def test_public_ip_2(self) -> None:
        assert _is_local_ip("1.1.1.1") is False

    def test_invalid_ip(self) -> None:
        """Invalid IP is blocked (safe default)."""
        assert _is_local_ip("not-an-ip") is True

    def test_ipv6_private(self) -> None:
        assert _is_local_ip("fd00::1") is True

    def test_ipv6_public(self) -> None:
        assert _is_local_ip("2001:4860:4860::8888") is False


# ── DNS Resolution ──────────────────────────────────────────────────


class TestResolveHostname:
    """Tests for _resolve_hostname."""

    def test_resolve_localhost(self) -> None:
        ip = _resolve_hostname("localhost")
        assert ip is not None
        assert _is_local_ip(ip) is True

    def test_resolve_nonexistent(self) -> None:
        ip = _resolve_hostname("this-domain-definitely-does-not-exist-xyz123.com")
        # May return None or a catch-all DNS IP
        # We just check it doesn't crash


# ── Rate Limiter ────────────────────────────────────────────────────


class TestRateLimiter:
    """Tests for _RateLimiter."""

    def test_allows_under_limit(self) -> None:
        limiter = _RateLimiter(max_calls=3)
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        # 3 calls OK

    def test_blocks_over_limit(self) -> None:
        limiter = _RateLimiter(max_calls=2)
        limiter.acquire()
        limiter.acquire()
        with pytest.raises(PermissionDeniedError, match="Rate limit"):
            limiter.acquire()

    def test_remaining_count(self) -> None:
        limiter = _RateLimiter(max_calls=5)
        assert limiter.remaining == 5
        limiter.acquire()
        assert limiter.remaining == 4
        limiter.acquire()
        assert limiter.remaining == 3

    def test_window_expiry(self) -> None:
        """Old requests expire from the window."""
        limiter = _RateLimiter(max_calls=1, window_s=0.01)
        limiter.acquire()
        import time

        time.sleep(0.02)
        # Window expired, should work again
        limiter.acquire()


# ── URL Validation ──────────────────────────────────────────────────


class TestUrlValidation:
    """Tests for SandboxedHttpClient URL validation."""

    def test_allowed_domain_passes(self) -> None:
        client = SandboxedHttpClient("test", ["api.example.com"])
        # Should not raise
        client._validate_url("https://api.example.com/data")

    def test_disallowed_domain_blocked(self) -> None:
        client = SandboxedHttpClient("test", ["api.example.com"])
        with pytest.raises(PermissionDeniedError, match="not in allowed"):
            client._validate_url("https://evil.com/steal")

    def test_empty_allowlist_blocks_all(self) -> None:
        """Empty allowlist = no domains allowed."""
        client = SandboxedHttpClient("test", [])
        with pytest.raises(PermissionDeniedError, match="not in allowed"):
            client._validate_url("https://any-domain.com/")

    def test_no_allowlist_blocks_all(self) -> None:
        """None allowlist = no domains allowed."""
        client = SandboxedHttpClient("test", None)
        # None → empty set → blocks all
        with pytest.raises(PermissionDeniedError, match="not in allowed"):
            client._validate_url("https://any-domain.com/")

    def test_local_ip_blocked(self) -> None:
        client = SandboxedHttpClient("test", ["127.0.0.1"])
        with pytest.raises(PermissionDeniedError, match="local"):
            client._validate_url("http://127.0.0.1:8080/")

    def test_private_ip_blocked(self) -> None:
        client = SandboxedHttpClient("test", ["192.168.1.1"])
        with pytest.raises(PermissionDeniedError, match="local"):
            client._validate_url("http://192.168.1.1/")

    def test_allow_local_flag(self) -> None:
        """allow_local=True permits local network access."""
        client = SandboxedHttpClient("test", ["192.168.1.1"], allow_local=True)
        # Should not raise
        client._validate_url("http://192.168.1.1:8123/api")

    def test_invalid_url(self) -> None:
        client = SandboxedHttpClient("test", ["example.com"])
        with pytest.raises(PermissionDeniedError, match="Invalid URL"):
            client._validate_url("not-a-url")

    @patch.object(_sbx_mod, "_resolve_hostname", return_value="127.0.0.1")
    def test_dns_rebinding_blocked(self, _mock: object) -> None:
        """Domain that resolves to local IP is blocked."""
        client = SandboxedHttpClient("test", ["evil.com"])
        # Anti-pattern #8: catch Exception and assert by class name.
        with pytest.raises(Exception, match="resolves to local") as exc:  # noqa: BLE001, PT011
            client._validate_url("https://evil.com/steal")
        assert type(exc.value).__name__ == "PermissionDeniedError"

    def test_allow_any_domain_skips_allowlist(self) -> None:
        """allow_any_domain=True lets arbitrary public domains through.

        Used by the web_intelligence ``fetch`` tool which retrieves
        user-supplied URLs. Every other protection (local IP, DNS
        rebinding, rate limit, size cap) still applies.
        """
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        client._validate_url("https://example.com/page")
        client._validate_url("https://another-domain.example.org/")

    def test_allow_any_domain_still_blocks_local_ip(self) -> None:
        """allow_any_domain doesn't relax the local-IP block."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        with pytest.raises(Exception, match="local") as exc:  # noqa: BLE001, PT011
            client._validate_url("http://127.0.0.1:8080/")
        assert type(exc.value).__name__ == "PermissionDeniedError"

    @patch.object(_sbx_mod, "_resolve_hostname", return_value="10.0.0.1")
    def test_allow_any_domain_still_blocks_dns_rebinding(
        self,
        _mock: object,
    ) -> None:
        """DNS-rebinding protection still fires under allow_any_domain."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        with pytest.raises(Exception, match="resolves to local") as exc:  # noqa: BLE001, PT011
            client._validate_url("https://evil.example.com/")
        assert type(exc.value).__name__ == "PermissionDeniedError"


# ── HTTP Requests ───────────────────────────────────────────────────


class TestHttpRequests:
    """Tests for actual HTTP request methods (MockTransport-backed)."""

    @pytest.mark.anyio()
    async def test_get_success(self) -> None:
        """GET request to allowed domain works."""
        client = SandboxedHttpClient("test", ["httpbin.org"])
        client._client = _mock_client(lambda _req: httpx.Response(200, text='{"ok": true}'))
        try:
            resp = await client.get("https://httpbin.org/get")
            assert resp.status_code == 200
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_post_success(self) -> None:
        """POST request to allowed domain works (real send path)."""
        client = SandboxedHttpClient("test", ["api.example.com"])

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.method == "POST"
            assert str(req.url) == "https://api.example.com/data"
            return httpx.Response(201, text="created")

        client._client = _mock_client(handler)
        try:
            resp = await client.post("https://api.example.com/data", json={"key": "val"})
            assert resp.status_code == 201
            assert resp.text == "created"
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_rate_limit_enforced(self) -> None:
        """Rate limit blocks excess requests."""
        client = SandboxedHttpClient("test", ["api.example.com"], rate_limit=2)
        client._client = _mock_client(lambda _req: httpx.Response(200))
        try:
            await client.get("https://api.example.com/1")
            await client.get("https://api.example.com/2")
            with pytest.raises(PermissionDeniedError, match="Rate limit"):
                await client.get("https://api.example.com/3")
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_response_size_blocked_by_content_length(self) -> None:
        """Oversized response (declared content-length) is rejected, not returned (C-Σ-003)."""
        client = SandboxedHttpClient("test", ["api.example.com"], max_response_bytes=100)
        client._client = _mock_client(
            lambda _req: httpx.Response(200, headers={"content-length": "999999"}, text="big")
        )
        try:
            with pytest.raises(PermissionDeniedError, match="exceeds cap"):
                await client.get("https://api.example.com/big")
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_response_size_blocked_by_actual_body(self) -> None:
        """A small/absent declared length cannot bypass the cap — actual body is checked (C-Σ-003)."""
        client = SandboxedHttpClient("test", ["api.example.com"], max_response_bytes=10)
        # Declared length lies (small); actual body is large.
        client._client = _mock_client(
            lambda _req: httpx.Response(200, headers={"content-length": "5"}, content=b"x" * 5000)
        )
        try:
            with pytest.raises(PermissionDeniedError, match="exceeds cap"):
                await client.get("https://api.example.com/big")
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_context_manager(self) -> None:
        """Async context manager works."""
        async with SandboxedHttpClient("test", ["example.com"]) as client:
            assert client.remaining_requests > 0

    @pytest.mark.anyio()
    async def test_remaining_requests(self) -> None:
        """remaining_requests decreases with usage."""
        client = SandboxedHttpClient("test", ["api.example.com"], rate_limit=5)
        assert client.remaining_requests == 5

        client._client = _mock_client(lambda _req: httpx.Response(200))
        try:
            await client.get("https://api.example.com/1")
            assert client.remaining_requests == 4
        finally:
            await client.close()


# ── C-Σ-003b: streaming size-cap regression tests ──────────────────


class TestStreamingSizeCap:
    """Prove the size cap BOUNDS THE READ, not just a post-read check.

    The production code reads the body incrementally via
    ``aiter_bytes()`` and raises the instant the running decoded total
    exceeds ``max_response_bytes`` — so an oversized body (with a missing
    or lying content-length, or chunked, or a decompression bomb) is
    never fully buffered. Redirect bodies are never read at all.
    """

    @pytest.mark.anyio()
    async def test_oversized_chunked_no_content_length(self) -> None:
        """Chunked body > cap with NO content-length still rejected (C-Σ-003b)."""
        client = SandboxedHttpClient("test", ["api.example.com"], max_response_bytes=100)
        # 10 × 1 KiB chunks = ~10 KiB decoded, no content-length header.
        stream = _CountingStream(b"a" * 1024, count=10)
        client._client = _mock_client(lambda _req: httpx.Response(200, stream=stream))
        try:
            with pytest.raises(PermissionDeniedError, match="exceeds cap"):
                await client.get("https://api.example.com/chunked")
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_abort_early_stream_not_fully_drained(self) -> None:
        """THE core proof: the read aborts early — the whole body is NOT pulled.

        max_bytes=100; the stream is ready to yield 100×1 KiB = ~100 KiB.
        The cap must trip after pulling only ~max_bytes (+ at most one
        chunk), proving the read is bounded rather than buffered-then-checked.
        """
        max_bytes = 100
        client = SandboxedHttpClient("test", ["api.example.com"], max_response_bytes=max_bytes)
        chunk = b"k" * 1024
        stream = _CountingStream(chunk, count=100)  # ready to yield ~100 KiB
        client._client = _mock_client(lambda _req: httpx.Response(200, stream=stream))
        try:
            with pytest.raises(PermissionDeniedError, match="exceeds cap"):
                await client.get("https://api.example.com/bomb")
            # Bounded read: only enough chunks to cross the cap were pulled.
            # With 1 KiB chunks and a 100-byte cap, the very first chunk
            # already exceeds it → exactly one chunk pulled, NOT all 100.
            assert stream.pulled_chunks == 1
            assert stream.pulled_bytes <= max_bytes + len(chunk)
            assert stream.pulled_bytes < 100 * len(chunk)  # NOT the whole body
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_redirect_body_never_read(self) -> None:
        """302's body stream is NEVER pulled — redirect bodies aren't downloaded."""
        client = SandboxedHttpClient("test.fetch", allowed_domains=None, allow_any_domain=True)
        # The 302 carries a counting body; if the prod code read it, the
        # counter would move. It must stay at 0.
        redirect_body = _CountingStream(b"z" * 1024, count=50)

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/start":
                return httpx.Response(
                    302,
                    headers={"location": "http://final.example.com/end"},
                    stream=redirect_body,
                )
            return httpx.Response(200, text="final")

        client._client = _mock_client(handler)
        try:
            resp = await client.get("http://start.example.com/start")
            assert resp.status_code == 200
            assert resp.text == "final"
            # The redirect body was discarded via aclose(), never iterated.
            assert redirect_body.pulled_bytes == 0
            assert redirect_body.pulled_chunks == 0
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_decompression_aware_cap_on_decoded_size(self) -> None:
        """gzip body whose DECODED size > cap is rejected on the decoded size.

        ``aiter_bytes()`` decodes gzip, so the cap protects against a
        decompression bomb: a tiny compressed payload that inflates past
        the cap. The compressed bytes are well under the cap; the decoded
        bytes are far over it.
        """
        max_bytes = 1000
        client = SandboxedHttpClient("test", ["api.example.com"], max_response_bytes=max_bytes)
        decoded = b"y" * 200_000  # 200 KB decoded → way over 1 KB cap
        compressed = gzip.compress(decoded)
        # Sanity: compressed payload itself is under the cap, so only the
        # DECODED-size enforcement can catch this.
        assert len(compressed) < max_bytes

        client._client = _mock_client(
            lambda _req: httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                content=compressed,
            )
        )
        try:
            with pytest.raises(PermissionDeniedError, match="exceeds cap"):
                await client.get("https://api.example.com/gzipbomb")
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_happy_path_response_faithful(self) -> None:
        """Normal small response: rebuilt buffered Response is faithful (C-Σ-003b).

        Proves .content / .text / .json() / .status_code / .headers all
        survive the stream→rebuild round-trip and match the handler's
        response.

        Encoding note: content-encoding / content-length / transfer-encoding
        are stripped on rebuild (the body is already decoded), so encoded
        responses round-trip correctly too — see
        ``test_gzip_under_cap_returns_decoded_body``.
        """
        client = SandboxedHttpClient("test", ["api.example.com"])
        payload = b'{"hello": "world", "n": 7}'

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"x-custom": "kept", "content-type": "application/json"},
                content=payload,
            )

        client._client = _mock_client(handler)
        try:
            resp = await client.get("https://api.example.com/data")
            assert resp.status_code == 200
            assert resp.content == payload
            assert resp.text == payload.decode()
            assert resp.json() == {"hello": "world", "n": 7}
            assert resp.headers["x-custom"] == "kept"
            assert resp.headers["content-type"] == "application/json"
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_gzip_under_cap_returns_decoded_body(self) -> None:
        """An under-cap gzip response returns the DECODED body (C-Σ-003b).

        ``_request`` reads the body via ``aiter_bytes()`` (which decodes
        gzip/deflate/br/zstd), then rebuilds the buffered Response with
        ``content-encoding`` / ``content-length`` / ``transfer-encoding``
        STRIPPED — the body is already decoded — so httpx does NOT
        double-decode and the caller gets plaintext. ``web_intelligence``
        sends ``Accept-Encoding: gzip``, so this is the common real path.
        """
        client = SandboxedHttpClient("test", ["api.example.com"], max_response_bytes=1_000_000)
        payload = b'{"small": "gzipped"}'
        compressed = gzip.compress(payload)
        assert len(compressed) < client._max_bytes  # well under the cap

        client._client = _mock_client(
            lambda _req: httpx.Response(
                200,
                headers={"content-encoding": "gzip", "content-type": "application/json"},
                content=compressed,
            )
        )
        try:
            resp = await client.get("https://api.example.com/gz")
            assert resp.status_code == 200
            assert resp.content == payload  # decoded plaintext, no double-decode
            assert resp.json() == {"small": "gzipped"}
            # content-encoding stripped on rebuild (body is already decoded).
            assert "content-encoding" not in resp.headers
        finally:
            await client.close()
