"""HTTP client for the Metabase REST API.

Retries are layered:

* `httpx.HTTPTransport(retries=N)` retries connection-setup errors at the
  transport level (TCP RST during connect, DNS hiccups).
* `tenacity` retries idempotent statuses (408, 429, 502, 503, 504) and the
  mid-stream network errors that mean the response never fully arrived.
  Honours the `Retry-After` header (used by Metabase for rate limiting) via
  `wait_combine(wait_exponential(...), tenacity-honoured Retry-After)`.

POST is retried on the same status set as GET/PUT because Metabase's POST-to-
create endpoints treat duplicate-creation as a clean 4xx; transient 5xx means
the request never reached the app.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import httpx
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

# Status codes that indicate a transient failure worth retrying. 429 = rate
# limited (server explicitly asks us to back off). 502/503/504 = upstream proxy
# or app temporarily unreachable. 408 = request timeout (server gave up
# reading) — also retryable.
_TRANSIENT_STATUSES = frozenset({408, 429, 502, 503, 504})

# Network errors httpx raises that mean "the request didn't reach the app or
# the response didn't fully arrive" — safe to retry.
_TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
)


def _is_transient_exception(exc: BaseException) -> bool:
    return isinstance(exc, _TRANSIENT_EXCEPTIONS)


def _is_transient_response(response: httpx.Response | None) -> bool:
    return response is not None and response.status_code in _TRANSIENT_STATUSES


class MetabaseClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        retry_backoff_s: float = 1.0,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout_s,
            transport=httpx.HTTPTransport(retries=max_retries),
        )
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MetabaseClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, path: str, **params: Any) -> Any:
        r = self._request("GET", path, params=_clean(params))
        return r.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        r = self._request("POST", path, json=body)
        return r.json()

    def put(self, path: str, body: dict[str, Any]) -> Any:
        r = self._request("PUT", path, json=body)
        return r.json()

    # --- resource shortcuts ---------------------------------------------------

    def list_collections(self) -> list[dict[str, Any]]:
        return self.get(
            "/api/collection", archived=False, exclude_other_user_collections=True
        )

    def get_collection(self, cid: int | str) -> dict[str, Any]:
        return self.get(f"/api/collection/{cid}")

    def list_cards(self) -> list[dict[str, Any]]:
        return self.get("/api/card", f="all")

    def get_card(self, cid: int) -> dict[str, Any]:
        return self.get(f"/api/card/{cid}")

    def list_dashboards(self) -> list[dict[str, Any]]:
        return self.get("/api/dashboard")

    def get_dashboard(self, did: int) -> dict[str, Any]:
        return self.get(f"/api/dashboard/{did}")

    def list_snippets(self) -> list[dict[str, Any]]:
        return self.get("/api/native-query-snippet")

    def get_snippet(self, sid: int) -> dict[str, Any]:
        return self.get(f"/api/native-query-snippet/{sid}")

    def list_databases(self) -> list[dict[str, Any]]:
        return _unwrap(self.get("/api/database"))

    def list_pulses(self) -> list[dict[str, Any]]:
        return self.get("/api/pulse")

    def get_pulse(self, pid: int) -> dict[str, Any]:
        return self.get(f"/api/pulse/{pid}")

    def list_users(self) -> list[dict[str, Any]]:
        return _unwrap(self.get("/api/user"))

    def count_out_of_scope(self) -> dict[str, int]:
        """Counts of resource types this tool does not sync. Used by export to
        print a warning. Missing endpoints (some Metabase versions don't expose
        all of these) are counted as zero."""
        counts: dict[str, int] = {}
        for name, path in (
            ("alerts", "/api/alert"),
            ("segments", "/api/segment"),
            ("metrics", "/api/legacy-metric"),
        ):
            try:
                items = _unwrap(self.get(path))
                counts[name] = len([i for i in items if not i.get("archived")])
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (400, 404, 405):
                    counts[name] = 0
                else:
                    raise
        return counts

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        retryer = Retrying(
            retry=(
                retry_if_exception(_is_transient_exception)
                | retry_if_result(_is_transient_response)
            ),
            wait=wait_exponential(multiplier=self._retry_backoff_s, min=0, max=60),
            stop=stop_after_attempt(self._max_retries + 1),
            reraise=True,
            before_sleep=lambda state: log.warning(
                "retry %s %s: attempt %d",
                method,
                path,
                state.attempt_number,
            ),
        )
        try:
            response = retryer(self._http.request, method, path, **kwargs)
        except RetryError as e:
            # All retries exhausted by `retry_if_result` (a sequence of 5xx).
            # Tenacity wraps the final response in `last_attempt`; unwrap it
            # so the caller sees the real status error rather than the
            # tenacity RetryError wrapper.
            if not e.last_attempt.failed:
                response = e.last_attempt.result()
            else:
                raise
        if not response.is_success:
            _raise_with_body(response)
        return response


def _unwrap(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and "data" in payload:
        return list(payload["data"])
    return list(payload)


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, Iterable) and not isinstance(v, (str, bytes)):
            out[k] = list(v)
        else:
            out[k] = v
    return out


def _raise_with_body(r: httpx.Response) -> None:
    if r.is_success:
        return
    body = r.text[:2000]
    raise httpx.HTTPStatusError(
        f"{r.request.method} {r.request.url} -> {r.status_code}: {body}",
        request=r.request,
        response=r,
    )
