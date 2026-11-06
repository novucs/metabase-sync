"""`main()` turns a MetabaseSyncError into a one-line message and exit 1, with
no Python traceback."""

from __future__ import annotations

import pytest

from metabase_sync import cli
from metabase_sync.errors import PreflightError


def test_sync_error_exits_1_without_traceback(monkeypatch, capsys):
    def boom() -> None:
        raise PreflightError("preflight failed: something")

    monkeypatch.setattr(cli, "app", boom)
    # Before the fix this raised click's Exit (not SystemExit), which Python
    # reports as an uncaught exception with a traceback.
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "preflight failed: something" in capsys.readouterr().err
