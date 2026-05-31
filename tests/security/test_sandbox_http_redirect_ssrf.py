"""SSRF hardening tests — Plugin Sandbox C1 (Round 3 paranoid audit).

Closes the ``follow_redirects=True`` SSRF bypass identified in v0.32.0:
``SandboxedHttpClient`` previously let httpx auto-follow 30x redirects
without re-running ``_validate_url``, so an attacker-controlled
allowlisted domain could 302 → ``http://169.254.169.254/`` (AWS
metadata) or any internal IP. The fix walks redirects manually and
validates EVERY hop's URL through the sandbox.

Each test wires an ``httpx.MockTransport`` into the sandbox client so
the REAL send / stream / manual-redirect-walk path is exercised
end-to-end without real network egress. ``follow_redirects=False`` is
preserved on the mock client — the SSRF closure depends on httpx NOT
auto-following 3xx so the sandbox can validate every hop itself.

Coverage:

* test_redirect_to_private_ip_rejected
* test_redirect_to_localhost_rejected
* test_redirect_chain_each_hop_validated
* test_legitimate_redirect_followed
* test_max_redirects_enforced
* test_redirect_method_downgrade_for_302
* test_redirect_method_preserved_for_307
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from sovyx.plugins.permissions import PermissionDeniedError
from sovyx.plugins.sandbox_http import SandboxedHttpClient


def _redirect_response(status: int, location: str) -> httpx.Response:
    """Build a minimal redirect response with the given Location header."""
    return httpx.Response(status_code=status, headers={"location": location})


def _ok_response(status: int = 200, *, text: str = "ok") -> httpx.Response:
    """Build a non-redirect terminal response."""
    return httpx.Response(status_code=status, text=text)


class _SequenceResponder:
    """MockTransport handler that returns a queued response per call.

    Wired into ``httpx.MockTransport`` and swapped onto
    ``client._client``, so it sees the REAL :class:`httpx.Request` that
    the production redirect walk builds for every hop (method downgrade,
    body/header stripping, and per-hop URL all happen for real before we
    observe them here). Each call pops the next queued response and
    records ``(method, url, kwargs)`` where ``kwargs`` is reconstructed
    from the actual request so the existing assertions keep working:

    * ``json``  → present (as the parsed JSON body) when the request
      carries a JSON content-type + body (mirrors the old ``json=`` kwarg).
    * ``data`` / ``content`` → present when a non-JSON body is sent.
    * ``headers`` → the request's headers as a plain ``dict`` (only the
      caller-supplied/meaningful keys are asserted on).

    The recorded URL is the absolute request URL string, exactly as the
    old mock recorded the ``url`` positional arg.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    @staticmethod
    def _reconstruct_kwargs(request: httpx.Request) -> dict[str, Any]:
        # Reconstruct headers from ``.raw`` so ORIGINAL casing is preserved
        # (httpx lowercases ``dict(headers)``; the downgrade test asserts the
        # caller-supplied ``X-Trace`` header survives by its exact name).
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in request.headers.raw}
        kwargs: dict[str, Any] = {"headers": headers}
        body = request.content
        if body:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                import json as _json

                kwargs["json"] = _json.loads(body)
            else:
                kwargs["content"] = body
        return kwargs

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), self._reconstruct_kwargs(request)))
        if not self._responses:
            raise AssertionError(
                f"_SequenceResponder ran out of queued responses (call #{len(self.calls)})"
            )
        return self._responses.pop(0)


def _install(client: SandboxedHttpClient, responder: _SequenceResponder) -> None:
    """Swap a MockTransport-backed client (follow_redirects=False) onto the sandbox.

    ``follow_redirects=False`` is CRITICAL: re-enabling auto-follow would
    let httpx walk 3xx WITHOUT the per-hop sandbox validation, silently
    masking the SSRF guard this whole module exists to prove.
    """
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        follow_redirects=False,
    )


# ── Core SSRF rejection ─────────────────────────────────────────────


class TestRedirectSsrfRejection:
    """Every redirect hop MUST re-enter ``_validate_url``."""

    @pytest.mark.anyio()
    async def test_redirect_to_private_ip_rejected(self) -> None:
        """302 → AWS metadata IP raises before the next request fires.

        Reproduces the audit's Plugin Sandbox C1 attack: attacker
        controls ``http://attacker.example.com/`` (allowlisted) and
        returns ``302 Location: http://169.254.169.254/latest/meta-data/``.
        """
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [_redirect_response(302, "http://169.254.169.254/latest/meta-data/")]
        )
        _install(client, responder)
        try:
            with pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"):
                await client.get("http://attacker.example.com/")
            # CRITICAL: only the FIRST request was issued — the metadata
            # endpoint was NEVER contacted.
            assert len(responder.calls) == 1
            assert responder.calls[0][1] == "http://attacker.example.com/"
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_redirect_to_localhost_rejected(self) -> None:
        """302 → 127.0.0.1 raises before the next request fires."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder([_redirect_response(302, "http://127.0.0.1:8080/")])
        _install(client, responder)
        try:
            with pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"):
                await client.get("http://attacker.example.com/")
            assert len(responder.calls) == 1
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_redirect_chain_each_hop_validated(self) -> None:
        """Multi-hop chain: each public hop OK, final internal hop rejected.

        Ensures the validator runs on EVERY hop, not just the first or
        last — an attacker chaining ``allowlisted → public → public →
        internal`` should still be blocked at the moment the internal
        hop is proposed.
        """
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(302, "http://hop2.example.com/"),
                _redirect_response(302, "http://hop3.example.com/"),
                _redirect_response(302, "http://10.0.0.5/admin"),
            ]
        )
        _install(client, responder)
        try:
            with pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"):
                await client.get("http://attacker.example.com/")
            # Three public hops fired, the fourth (to 10.0.0.5) was
            # blocked BEFORE the request was issued.
            assert len(responder.calls) == 3
            assert responder.calls[0][1] == "http://attacker.example.com/"
            assert responder.calls[1][1] == "http://hop2.example.com/"
            assert responder.calls[2][1] == "http://hop3.example.com/"
        finally:
            await client.close()


# ── Legitimate redirects + bounds ──────────────────────────────────


class TestRedirectLegitimate:
    """Sandbox MUST still follow legitimate public-to-public redirects."""

    @pytest.mark.anyio()
    async def test_legitimate_redirect_followed(self) -> None:
        """Public 302 → public, both pass validation, terminal response returned."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(302, "http://final.example.com/page"),
                _ok_response(200, text="hello"),
            ]
        )
        _install(client, responder)
        try:
            resp = await client.get("http://start.example.com/")
            assert resp.status_code == 200
            assert resp.text == "hello"
            assert len(responder.calls) == 2
            assert responder.calls[1][1] == "http://final.example.com/page"
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_max_redirects_enforced(self) -> None:
        """Infinite-redirect loop is bounded at _MAX_REDIRECTS hops."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        # 7 redirect responses available; hop limit is 5 so the 6th
        # response is the "max redirects" trigger.
        responder = _SequenceResponder(
            [_redirect_response(302, "http://loop.example.com/") for _ in range(10)]
        )
        _install(client, responder)
        try:
            with pytest.raises(PermissionDeniedError, match="max redirects"):
                await client.get("http://loop.example.com/")
            # 1 initial + 5 follow-ups = 6 actual requests, then the
            # 7th proposed hop trips the cap.
            assert len(responder.calls) == 6
        finally:
            await client.close()


# ── Method semantics on redirect ───────────────────────────────────


class TestRedirectMethodSemantics:
    """Method downgrade matches Python ``requests`` library."""

    @pytest.mark.anyio()
    async def test_redirect_method_downgrade_for_302(self) -> None:
        """POST → 302 → GET (body + body headers stripped).

        Defends against the POST-allowlisted-then-302 attack pattern.
        """
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(302, "http://final.example.com/safe"),
                _ok_response(200, text="downgraded"),
            ]
        )
        _install(client, responder)
        try:
            resp = await client.post(
                "http://start.example.com/",
                json={"secret": "value"},
                headers={"Content-Type": "application/json", "X-Trace": "keep"},
            )
            assert resp.status_code == 200
            assert len(responder.calls) == 2
            initial_method, _, initial_kwargs = responder.calls[0]
            redirect_method, redirect_url, redirect_kwargs = responder.calls[1]

            assert initial_method == "POST"
            assert "json" in initial_kwargs

            # Method downgraded to GET.
            assert redirect_method == "GET"
            assert redirect_url == "http://final.example.com/safe"
            # Body stripped.
            assert "json" not in redirect_kwargs
            assert "data" not in redirect_kwargs
            assert "content" not in redirect_kwargs
            # Body-describing headers stripped.
            stripped_headers = redirect_kwargs.get("headers", {})
            assert isinstance(stripped_headers, dict)
            lowered = {str(k).lower() for k in stripped_headers}
            assert "content-type" not in lowered
            assert "content-length" not in lowered
            # Non-body headers kept.
            assert "X-Trace" in stripped_headers
        finally:
            await client.close()

    @pytest.mark.anyio()
    async def test_redirect_method_preserved_for_307(self) -> None:
        """POST → 307 → POST (method + body preserved per RFC 7538)."""
        client = SandboxedHttpClient(
            "test.fetch",
            allowed_domains=None,
            allow_any_domain=True,
        )
        responder = _SequenceResponder(
            [
                _redirect_response(307, "http://final.example.com/safe"),
                _ok_response(200, text="preserved"),
            ]
        )
        _install(client, responder)
        try:
            resp = await client.post(
                "http://start.example.com/",
                json={"payload": "kept"},
            )
            assert resp.status_code == 200
            assert len(responder.calls) == 2
            redirect_method, _, redirect_kwargs = responder.calls[1]
            # Method + body preserved.
            assert redirect_method == "POST"
            assert redirect_kwargs.get("json") == {"payload": "kept"}
        finally:
            await client.close()


# ── Belt-and-suspenders: client construction ───────────────────────


class TestClientConstruction:
    """The httpx client must be built with follow_redirects=False."""

    def test_follow_redirects_disabled(self) -> None:
        """SSRF closure depends on ``follow_redirects=False`` — assert it.

        A future refactor that re-enables auto-follow reintroduces the
        v0.32.0 SSRF bypass. This test pins the construction invariant
        so that mistake is caught at unit-test time, not in production.
        """
        client = SandboxedHttpClient("guard", ["example.com"])
        # httpx exposes the configured value as ``follow_redirects`` on
        # the client instance.
        assert client._client.follow_redirects is False


# ── Coverage of public ``request`` and unmocked AsyncClient flow ────


class TestPublicRequestEntrypoint:
    """``client.request(method, ...)`` also walks the redirect chain."""

    @pytest.mark.anyio()
    async def test_request_method_redirect_validated(self) -> None:
        """Plugins using arbitrary verbs (PROPFIND, …) get the same guard."""
        client = SandboxedHttpClient("caldav.test", ["caldav.example.com"])
        responder = _SequenceResponder([_redirect_response(302, "http://192.168.1.1/internal")])
        _install(client, responder)
        try:
            with pytest.raises(PermissionDeniedError, match="redirect to unsafe URL"):
                await client.request("PROPFIND", "https://caldav.example.com/dav/")
            assert len(responder.calls) == 1
            assert responder.calls[0][0] == "PROPFIND"
        finally:
            await client.close()


# ── Sanity: the MockTransport harness records real requests ────────


class TestSequenceResponderHarness:
    """Smoke-test the test harness itself — fail-fast on broken mocks."""

    @pytest.mark.anyio()
    async def test_responder_records_calls(self) -> None:
        """The responder, wired via MockTransport, records the REAL request.

        Mirrors the wiring used by every SSRF test above: a sandbox
        client whose ``_client`` is a MockTransport-backed
        ``follow_redirects=False`` client. Sending one request records
        the actual ``(method, url, kwargs)`` the production code built.
        """
        responder = _SequenceResponder([_ok_response()])
        client = SandboxedHttpClient("harness.test", allowed_domains=None, allow_any_domain=True)
        _install(client, responder)
        try:
            result = await client.get("https://harness.example.com/", headers={"X-A": "b"})
            assert result.status_code == 200
            method, url, kwargs = responder.calls[0]
            assert method == "GET"
            assert url == "https://harness.example.com/"
            assert kwargs["headers"]["X-A"] == "b"
        finally:
            await client.close()
