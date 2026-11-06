"""`--collection` scope: resolution by entity_id or top-level name, subtree
filtering at the client, and the sticky-scope guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from metabase_sync.errors import ScopeError
from metabase_sync.scope import (
    ScopedClient,
    open_scope,
    read_scope,
    resolve,
    write_scope,
)

COLS = [
    {"id": 1, "entity_id": "E1", "name": "Repo A", "location": "/"},
    {"id": 2, "entity_id": "E2", "name": "Reports", "location": "/1/"},
    {"id": 3, "entity_id": "E3", "name": "Deep", "location": "/1/2/"},
    {"id": 4, "entity_id": "E4", "name": "Repo B", "location": "/"},
    {"id": 5, "entity_id": "E5", "name": "Reports", "location": "/4/"},
    {
        "id": 6,
        "entity_id": "E6",
        "name": "Repo A",
        "location": "/",
        "personal_owner_id": 9,
    },
    {"id": "root", "name": "Our analytics"},
]


def _client() -> MagicMock:
    c = MagicMock()
    c.list_collections.return_value = COLS
    c.list_cards.return_value = [
        {"id": 10, "collection_id": 3},
        {"id": 11, "collection_id": 4},
        {"id": 12, "collection_id": None},
    ]
    c.list_dashboards.return_value = [
        {"id": 20, "collection_id": 2},
        {"id": 21, "collection_id": 5},
    ]
    c.list_pulses.return_value = [
        {"id": 30, "dashboard_id": 20},
        {"id": 31, "dashboard_id": 21},
        {"id": 32, "dashboard_id": None},
    ]
    return c


def _state(tmp_path: Path, *dirs: tuple[str, str | None]) -> Path:
    for name, eid in dirs:
        d = tmp_path / "collections" / name
        d.mkdir(parents=True)
        (d / "_collection.yaml").write_text(
            f"entity_id: {eid or 'null'}\nname: {name}\n"
        )
    return tmp_path


def test_resolve_entity_id_then_top_level_name():
    assert resolve(COLS, "E4")["id"] == 4
    assert resolve(COLS, "Repo B")["id"] == 4
    assert resolve(COLS, "Repo A")["id"] == 1  # personal namesake ignored
    assert resolve(COLS, "Reports") is None  # nested names never match
    assert resolve(COLS, "E2") is None  # nested entity_id: top-level only


def test_resolve_refuses_duplicate_top_level_names():
    dup = COLS + [{"id": 7, "entity_id": "E7", "name": "Repo B", "location": "/"}]
    with pytest.raises(ScopeError, match="E4, E7"):
        resolve(dup, "Repo B")


def test_scoped_client_lists_only_subtree():
    c = _client()
    s = ScopedClient(c, resolve(COLS, "E1"), COLS)
    assert [x["id"] for x in s.list_collections()] == [1, 2, 3]
    assert [x["id"] for x in s.list_cards()] == [10]
    assert [x["id"] for x in s.list_dashboards()] == [20]
    assert [x["id"] for x in s.list_pulses()] == [30]
    assert s.list_snippets() == []
    s.get_card(10)  # detail calls pass through
    c.get_card.assert_called_once_with(10)


def test_missing_target_lists_nothing_without_calls():
    c = _client()
    s = ScopedClient(c, None, COLS)
    assert s.list_cards() == [] and s.list_dashboards() == [] and s.list_pulses() == []
    c.list_cards.assert_not_called()
    c.list_dashboards.assert_not_called()


def test_unscoped_returns_client_unchanged(tmp_path: Path):
    c = _client()
    assert open_scope(c, tmp_path, None, export=False) == (c, None)
    c.list_collections.assert_not_called()


def test_saved_scope_is_sticky(tmp_path: Path):
    write_scope(_state(tmp_path, ("repo-a", "E1")), "E1")
    s, target = open_scope(_client(), tmp_path, None, export=False)
    assert isinstance(s, ScopedClient) and target["id"] == 1
    assert read_scope(tmp_path) == "E1"


def test_flag_by_name_matches_saved_entity_id(tmp_path: Path):
    write_scope(_state(tmp_path, ("repo-a", "E1")), "E1")
    _, target = open_scope(_client(), tmp_path, "Repo A", export=False)
    assert target["id"] == 1


def test_flag_conflicting_with_saved_scope_refused(tmp_path: Path):
    write_scope(_state(tmp_path, ("repo-a", "E1")), "E1")
    with pytest.raises(ScopeError, match="does not match the scope saved"):
        open_scope(_client(), tmp_path, "Repo B", export=False)


def test_export_needs_existing_target(tmp_path: Path):
    with pytest.raises(ScopeError, match="run apply to create it"):
        open_scope(_client(), tmp_path, "New Repo", export=True)


def test_first_apply_allowed_for_missing_target(tmp_path: Path):
    _, target = open_scope(
        _client(), _state(tmp_path, ("new", None)), "New Repo", export=False
    )
    assert target is None


def test_scoped_state_with_two_roots_refused(tmp_path: Path):
    _state(tmp_path, ("repo-a", "E1"), ("repo-b", "E4"))
    with pytest.raises(ScopeError, match="exactly one top-level collection"):
        open_scope(_client(), tmp_path, "E1", export=False)


def test_state_holding_other_collection_refused(tmp_path: Path):
    _state(tmp_path, ("repo-b", "E4"))
    with pytest.raises(ScopeError, match="holds collection E4"):
        open_scope(_client(), tmp_path, "E1", export=False)
