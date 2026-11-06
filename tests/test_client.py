import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from metabase_sync.errors import MetabaseSyncError, UnsupportedVersionError
from metabase_sync.format.api import VERSIONS, for_version
from metabase_sync.metabase.client import MetabaseClient, UncertainWriteError
from metabase_sync.settings import Settings
from metabase_sync.store import yamlio

Reply = tuple[int, object] | None


@pytest.fixture
def serve() -> Iterator[
    Callable[[Callable[[str, int], Reply]], tuple[MetabaseClient, list[str]]]
]:
    servers: list[ThreadingHTTPServer] = []

    def start(reply: Callable[[str, int], Reply]) -> tuple[MetabaseClient, list[str]]:
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def handle_one(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                seen.append(self.command)
                answer = reply(self.command, len(seen))
                if answer is None:
                    self.close_connection = True
                    return
                status, body = answer
                data = json.dumps(body).encode()
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "http://elsewhere.test/api/card")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = handle_one

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        servers.append(server)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        settings = Settings.model_validate({"url": url, "api_key": "k"})
        return MetabaseClient(settings), seen

    yield start
    for server in servers:
        server.shutdown()


def drop_first(answer: Reply) -> Callable[[str, int], Reply]:
    return lambda method, n: None if n == 1 else answer


def test_reads_are_retried_after_a_dropped_connection(serve):
    client, seen = serve(drop_first((200, {"ok": True})))

    assert client.get("/api/card") == {"ok": True}
    assert seen == ["GET", "GET"]


def test_a_create_is_never_resent_when_its_outcome_is_unknown(serve):
    client, seen = serve(drop_first((200, {"id": 1})))

    with pytest.raises(UncertainWriteError):
        client.post("/api/card", {"name": "x"})
    assert seen == ["POST"]


def test_a_create_that_cannot_connect_is_not_reported_as_uncertain():
    settings = Settings.model_validate({"url": "http://127.0.0.1:1", "api_key": "k"})
    client = MetabaseClient(settings, retries=0)

    with pytest.raises(MetabaseSyncError) as raised:
        client.post("/api/card", {"name": "x"})
    assert not isinstance(raised.value, UncertainWriteError)


def test_parallel_reads_report_progress_and_keep_their_order():
    settings = Settings.model_validate({"url": "http://mb.test", "api_key": "k"})
    client = MetabaseClient(settings)
    reported: list[str] = []
    others_counted = threading.Event()

    def report(text: str) -> None:
        reported.append(text)
        if text == "Reading 2/3":
            others_counted.set()

    client.report = report
    first_waited: list[bool] = []

    def read(item: int) -> int:
        if item == 0:
            first_waited.append(others_counted.wait(timeout=5))
        return item * 10

    assert client.map(read, [0, 1, 2], label="Reading") == [0, 10, 20]
    assert first_waited == [True]
    assert reported == ["Reading 0/3", "Reading 1/3", "Reading 2/3", "Reading 3/3"]


def test_a_delete_whose_retry_finds_nothing_has_succeeded(serve):
    client, seen = serve(drop_first((404, {"message": "Not found."})))

    assert client.delete("/api/card/5") is None
    assert seen == ["DELETE", "DELETE"]


def test_a_redirect_is_reported_instead_of_followed_with_the_api_key(serve):
    client, seen = serve(lambda m, n: (302, {}))

    with pytest.raises(MetabaseSyncError) as raised:
        client.get("/api/card")

    assert "http://elsewhere.test/api/card" in str(raised.value)
    assert seen == ["GET"]


def test_enterprise_versions_are_checked_like_open_source_ones(serve, caplog):
    old, _ = serve(lambda m, n: (200, {"version": {"tag": "v1.57.3"}}))
    new, _ = serve(lambda m, n: (200, {"version": {"tag": "v1.64.4"}}))

    with pytest.raises(UnsupportedVersionError):
        for_version(*old.supported_version)
    assert for_version(*new.supported_version) is VERSIONS[64]
    assert not caplog.records


def test_ci_runs_the_integration_suite_on_every_tested_version():
    root = Path(__file__).parent.parent
    workflow = yamlio.read(root / ".github" / "workflows" / "ci.yml")
    images = workflow["jobs"]["integration"]["strategy"]["matrix"]["metabase"]

    assert sorted(int(image.split(".")[1]) for image in images) == sorted(VERSIONS)
