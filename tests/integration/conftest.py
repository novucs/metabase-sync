import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.settings import Settings

from ._mb import MetabaseAdmin

COMPOSE_FILE = Path(__file__).parent / "docker-compose.yml"
WORKER = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
PROJECT = f"metabase-sync-test-{WORKER}"
PORT = int(os.environ.get("METABASE_PORT", "3000")) + int(WORKER.removeprefix("gw"))
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "tester-password-123"


def _url() -> str:
    return f"http://127.0.0.1:{PORT}"


def _docker(*cmd: str) -> None:
    full = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *cmd]
    env = {**os.environ, "METABASE_PORT": str(PORT)}
    result = subprocess.run(full, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(full)} exited {result.returncode}\n{result.stdout}\n{result.stderr}"
        )


def _wait_until_ready(url: str, timeout_s: int = 300) -> None:
    deadline = time.monotonic() + timeout_s
    last = "no attempts"
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{url}/api/health", timeout=5)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return
            last = f"{r.status_code} {r.text[:120]}"
        except (requests.RequestException, ValueError) as e:
            last = repr(e)
        time.sleep(0.5)
    raise RuntimeError(f"Metabase did not become healthy: {last}")


def _setup_admin(url: str) -> str:
    props = requests.get(f"{url}/api/session/properties", timeout=10).json()
    if not props.get("setup-token"):
        raise RuntimeError(
            "Metabase is already set up; use METABASE_INTEGRATION_SKIP_COMPOSE"
        )
    body = {
        "token": props["setup-token"],
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
    r = requests.post(f"{url}/api/setup", json=body, timeout=60)
    r.raise_for_status()
    session = r.json()["id"]
    r = requests.post(
        f"{url}/api/api-key",
        json={"name": "metabase-sync-test", "group_id": 2},
        headers={"X-Metabase-Session": session},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["unmasked_key"]


@pytest.fixture(scope="session")
def metabase_url_and_key() -> Iterator[tuple[str, str]]:
    if os.environ.get("METABASE_INTEGRATION_SKIP_COMPOSE") == "1":
        yield os.environ["METABASE_URL"], os.environ["METABASE_API_KEY"]
        return
    url = _url()
    _docker("up", "-d")
    try:
        _wait_until_ready(url)
        yield url, _setup_admin(url)
    finally:
        _docker("down", "-v")


@pytest.fixture
def mb(metabase_url_and_key: tuple[str, str]) -> Iterator[MetabaseAdmin]:
    admin = MetabaseAdmin(*metabase_url_and_key)
    try:
        yield admin
    finally:
        admin.close()


@pytest.fixture
def reversed_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    def run_backwards(self: MetabaseClient, fn: Any, items: Any) -> list[Any]:
        items = list(items)
        return [fn(i) for i in reversed(items)][::-1]

    monkeypatch.setattr(MetabaseClient, "map", run_backwards)


@pytest.fixture
def client(metabase_url_and_key: tuple[str, str]) -> Iterator[MetabaseClient]:
    url, key = metabase_url_and_key
    with MetabaseClient(Settings(url=url, api_key=key)) as c:
        yield c
