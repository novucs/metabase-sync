"""HTTP client retries: a single transient failure must not abort an apply
mid-way through. Tests use httpx.MockTransport to inject scripted responses."""

from __future__ import annotations

from collections.abc import Iterable

import httpx
import pytest

from metabase_sync.client import MetabaseClient


def _client_with_responses(responses: Iterable[httpx.Response]) -> MetabaseClient:
    """Build a MetabaseClient whose underlying transport returns `responses` in
    order. Disables real sleep so the test is fast."""
    iterator = iter(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(iterator)

    c = MetabaseClient(
        "http://example.test",
        "key",
        timeout_s=1.0,
        max_retries=3,
        retry_backoff_s=0.0,
    )
    c._http = httpx.Client(  # noqa: SLF001 — wiring a mock transport
        base_url="http://example.test",
        headers={"X-API-Key": "key", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    return c


def _ok(body: dict) -> httpx.Response:
    return httpx.Response(200, json=body)


def _transient(status: int) -> httpx.Response:
    return httpx.Response(status, text="transient")


def test_get_retries_on_503_then_succeeds():
    with _client_with_responses(
        [_transient(503), _transient(503), _ok({"ok": 1})]
    ) as c:
        assert c.get("/api/health") == {"ok": 1}


def test_get_retries_on_429():
    with _client_with_responses([_transient(429), _ok({"ok": 1})]) as c:
        assert c.get("/api/card/1") == {"ok": 1}


def test_put_retries_on_502():
    with _client_with_responses([_transient(502), _ok({"id": 9})]) as c:
        assert c.put("/api/dashboard/9", {"name": "x"}) == {"id": 9}


def test_post_retries_on_504():
    with _client_with_responses([_transient(504), _ok({"id": 1})]) as c:
        assert c.post("/api/card", {"name": "x"}) == {"id": 1}


def test_persistent_5xx_eventually_raises():
    with _client_with_responses([_transient(503)] * 5) as c:
        with pytest.raises(httpx.HTTPStatusError):
            c.get("/api/health")


def test_4xx_is_not_retried():
    """Client errors mean the server processed and rejected the request — no
    point retrying."""
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    c = MetabaseClient(
        "http://example.test",
        "key",
        timeout_s=1.0,
        max_retries=3,
        retry_backoff_s=0.0,
    )
    c._http = httpx.Client(  # noqa: SLF001
        base_url="http://example.test",
        headers={"X-API-Key": "key", "Accept": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            c.get("/api/card/123")
    finally:
        c.close()
    assert calls["n"] == 1
