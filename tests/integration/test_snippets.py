"""Snippet integration tests: root-level round-trip + in-collection routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from metabase_sync.apply import run as run_apply
from metabase_sync.client import MetabaseClient
from metabase_sync.export import run_export

from ._builders import unique_name, write_collection, write_snippet
from ._mb import MetabaseAdmin

pytestmark = pytest.mark.integration


def _apply(url: str, key: str, state: Path, **kw):
    with MetabaseClient(url, key) as client:
        return run_apply(client, state, **kw)


def test_root_snippet_round_trip(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    name = unique_name("weekday-filter")
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
    write_snippet(state, "weekday", name, "1 = 1")

    applied = _apply(metabase_url, metabase_api_key, state, mode="apply")
    assert any(
        c.action == "create" and c.resource == "snippets" for c in applied.changes
    )
    listed = mb.get("/api/native-query-snippet")
    assert any(s["name"] == name for s in listed)


def test_snippet_in_collection_is_flattened_safely(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Snippet folders aren't supported (Metabase keeps them in a separate
    `:snippets` namespace). A snippet authored under a collection folder must
    NOT abort the apply with a 400 — it applies to the root snippets namespace
    instead. Guards against the namespace 400 landmine."""
    coll_name = unique_name("snip-coll")
    snip_name = unique_name("nested-snippet")
    state = tmp_path / "state"
    write_collection(state, "snipcoll", coll_name)
    write_snippet(state, "nested", snip_name, "2 = 2", coll_slug="snipcoll")

    _apply(metabase_url, metabase_api_key, state, mode="apply")

    snippet = next(
        s for s in mb.get("/api/native-query-snippet") if s["name"] == snip_name
    )
    assert snippet["collection_id"] is None  # flattened to root, not rejected
