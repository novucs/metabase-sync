"""Session fixture that brings up a real Metabase via docker compose, completes
first-boot setup, mints an admin API key, and yields (url, api_key) for tests.

Tear-down: `docker compose down -v` so each test session starts from scratch.

Costs ~60-90s of Metabase boot per session. Mark every test with
`@pytest.mark.integration` so the fast unit suite (`pytest --ignore=tests/integration`)
doesn't pay this.

## Shared-mutable-server contract

There is ONE Metabase per pytest session and NO per-test reset (a full reset
would multiply the boot cost across the suite). Consequences every test must
respect:

* Author your objects in a UNIQUELY-named collection — use `_unique_name(prefix)`
  so two tests (or two runs against a warm instance) never collide.
* Scope assertions to your own objects, not global counts. The one safe
  exception is whole-server no-op checks (export the whole tree, then plan/apply
  and assert zero writes) — those are inherently consistent because disk always
  reflects the server state they were exported from.
* Don't assume the instance is empty. `test_export_against_empty` only holds if
  it runs first; treat it as "export a fresh-ish instance" rather than a
  guarantee.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from ._mb import MetabaseAdmin

COMPOSE_FILE = Path(__file__).parent / "docker-compose.yml"
METABASE_URL = "http://localhost:3000"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "tester-password-123"


def _docker(cmd: list[str]) -> None:
    full = ["docker", "compose", "-f", str(COMPOSE_FILE), *cmd]
    result = subprocess.run(full, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(full)}` exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )


def _wait_until_ready(timeout_s: int = 240) -> None:
    deadline = time.monotonic() + timeout_s
    last_err = "no attempts made"
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{METABASE_URL}/api/health", timeout=5)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return
            last_err = f"{r.status_code} {r.text[:120]}"
        except httpx.HTTPError as e:
            last_err = repr(e)
        time.sleep(2)
    raise RuntimeError(f"Metabase did not become healthy in {timeout_s}s: {last_err}")


def _setup_admin() -> str:
    """Return an admin session token from the first-boot setup."""
    props = httpx.get(f"{METABASE_URL}/api/session/properties", timeout=10).json()
    setup_token = props.get("setup-token")
    if not setup_token:
        raise RuntimeError("Metabase has no setup-token; instance is already set up")
    body = {
        "token": setup_token,
        "prefs": {"site_name": "test", "site_locale": "en", "allow_tracking": False},
        "user": {
            "first_name": "Test",
            "last_name": "Admin",
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "password_confirm": ADMIN_PASSWORD,
            "site_name": "test",
        },
        "database": None,
    }
    r = httpx.post(f"{METABASE_URL}/api/setup", json=body, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def _mint_api_key(session_token: str) -> str:
    headers = {"X-Metabase-Session": session_token}
    # Admin group is always id 2 on a fresh install.
    body = {"name": "metabase-sync-test", "group_id": 2}
    r = httpx.post(
        f"{METABASE_URL}/api/api-key", json=body, headers=headers, timeout=10
    )
    r.raise_for_status()
    return r.json()["unmasked_key"]


@pytest.fixture(scope="session")
def metabase_url_and_key() -> Iterator[tuple[str, str]]:
    if os.environ.get("METABASE_INTEGRATION_SKIP_COMPOSE") == "1":
        yield os.environ["METABASE_URL"], os.environ["METABASE_API_KEY"]
        return

    _docker(["up", "-d"])
    try:
        _wait_until_ready()
        session_token = _setup_admin()
        api_key = _mint_api_key(session_token)
        yield METABASE_URL, api_key
    finally:
        _docker(["down", "-v"])


@pytest.fixture(scope="session")
def metabase_url(metabase_url_and_key: tuple[str, str]) -> str:
    return metabase_url_and_key[0]


@pytest.fixture(scope="session")
def metabase_api_key(metabase_url_and_key: tuple[str, str]) -> str:
    return metabase_url_and_key[1]


@pytest.fixture
def mb(metabase_url_and_key: tuple[str, str]) -> Iterator[MetabaseAdmin]:
    """Per-test admin client for server-side setup (create cards, dashboards…)."""
    admin = MetabaseAdmin(*metabase_url_and_key)
    try:
        yield admin
    finally:
        admin.close()
