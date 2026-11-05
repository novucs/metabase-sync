import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import cached_property
from typing import Any, Self

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError
from urllib3.util.retry import Retry

from metabase_sync.errors import ApiError, MetabaseSyncError, UnsupportedVersionError
from metabase_sync.settings import Settings

log = logging.getLogger(__name__)

TESTED = (58, 64, 65)
UNCERTAIN_STATUSES = {502, 503, 504}
AUTH_HINTS = {
    401: "Check that METABASE_API_KEY (or METABASE_HEADERS) is valid for this Metabase",
    403: "metabase-sync needs an API key in the Administrators group",
}


class UncertainWriteError(MetabaseSyncError):
    pass


def _never_sent(error: requests.RequestException) -> bool:
    cause = error.args[0] if error.args else None
    return isinstance(error, requests.ConnectTimeout) or (
        isinstance(cause, MaxRetryError)
        and isinstance(cause.reason, ConnectTimeoutError)
    )


class MetabaseClient:
    def __init__(self, settings: Settings, retries: int = 3) -> None:
        self.settings = settings
        self._base = settings.base
        self._http = requests.Session()
        self._http.headers.update({"Accept": "application/json", **settings.headers})
        if settings.api_key:
            self._http.headers["X-API-Key"] = settings.api_key.get_secret_value()
        retry = Retry(
            total=retries,
            backoff_factor=1,
            status_forcelist=[429, *UNCERTAIN_STATUSES],
            allowed_methods=["GET", "PUT", "DELETE"],
            raise_on_status=False,
        )
        self._http.mount(
            settings.base,
            HTTPAdapter(pool_maxsize=settings.concurrency, max_retries=retry),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get(self, path: str, **params: Any) -> Any:
        return self._send("GET", path, params=_params(params))

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._send("PUT", path, json=body)

    def delete(self, path: str) -> Any:
        return self._send("DELETE", path)

    def post(self, path: str, body: dict[str, Any], read_only: bool = False) -> Any:
        return self._send("POST", path, read_only=read_only, json=body)

    def map[T, R](self, fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
        items = list(items)
        if len(items) <= 1:
            return [fn(i) for i in items]
        with ThreadPoolExecutor(max_workers=self.settings.concurrency) as pool:
            return list(pool.map(fn, items))

    def version(self) -> tuple[int, int, str]:
        tag = (self.get("/api/session/properties").get("version") or {}).get(
            "tag"
        ) or ""
        parts = tag.lstrip("v").split(".")
        try:
            edition, minor = int(parts[0]), int(parts[1])
        except (IndexError, ValueError) as e:
            raise UnsupportedVersionError(
                f"Cannot read the Metabase version from {tag!r}"
            ) from e
        if edition not in (0, 1):
            raise UnsupportedVersionError(f"Unknown Metabase version {tag!r}")
        return 0, minor, tag

    @cached_property
    def supported_version(self) -> tuple[int, str]:
        _, minor, tag = self.version()
        oldest = TESTED[0]
        if minor < oldest:
            raise UnsupportedVersionError(
                f"Metabase {tag} is older than 0.{oldest} (1.{oldest} for Enterprise)",
                hint="Use metabase-sync 0.3.x for Metabase 0.55 to 0.57",
            )
        if minor not in TESTED:
            log.warning(
                "Metabase %s is untested; metabase-sync is tested on %s",
                tag,
                ", ".join(f"0.{v}" for v in TESTED),
            )
        return minor, tag

    def _send(
        self, method: str, path: str, read_only: bool = False, **kwargs: Any
    ) -> Any:
        is_create = method == "POST" and not read_only
        try:
            response = self._http.request(
                method, self._base + path, timeout=self.settings.timeout, **kwargs
            )
        except requests.RequestException as e:
            if is_create and not _never_sent(e):
                raise UncertainWriteError(
                    f"{method} {path}: outcome unknown ({e})"
                ) from e
            if _never_sent(e):
                raise MetabaseSyncError(
                    f"Can't reach Metabase at {self._base}",
                    hint="Check METABASE_URL, and that Metabase is running and reachable",
                ) from e
            if isinstance(e, requests.Timeout):
                raise MetabaseSyncError(
                    f"Metabase took longer than {self.settings.timeout:g}s to answer {method} {path}",
                    hint="Raise METABASE_TIMEOUT if Metabase is slow",
                ) from e
            raise MetabaseSyncError(
                f"Lost the connection to Metabase during {method} {path}"
            ) from e
        if is_create and response.status_code in UNCERTAIN_STATUSES:
            raise UncertainWriteError(
                f"{method} {path}: outcome unknown (HTTP {response.status_code})"
            )
        retried = bool(response.raw.retries and response.raw.retries.history)
        if method == "DELETE" and retried and response.status_code == 404:
            return None
        if response.status_code >= 400:
            error = ApiError(method, path, response.status_code, _error_text(response))
            error.hint = AUTH_HINTS.get(response.status_code)
            raise error
        if not response.content:
            return None
        return response.json()


def _params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        out[key] = str(value).lower() if isinstance(value, bool) else value
    return out


def _error_text(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        for key in ("message", "error", "errors", "specific-errors"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]
