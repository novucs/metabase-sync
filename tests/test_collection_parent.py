"""Metabase <= v0.55 lists collections without parent_id; the client derives it
from `location` so nested collections keep their place on export and plan."""

from __future__ import annotations

from metabase_sync.client import MetabaseClient


def test_parent_id_derived_from_location(monkeypatch):
    rows = [
        {"id": "root", "name": "Our analytics"},
        {"id": 1, "name": "top", "location": "/"},
        {"id": 2, "name": "sub", "location": "/1/"},
        {"id": 3, "name": "deep", "location": "/1/2/"},
        {"id": 4, "name": "given", "location": "/1/", "parent_id": 1},
    ]
    client = MetabaseClient("http://mb.example", "k")
    monkeypatch.setattr(client, "get", lambda *a, **k: rows)
    by_id = {c["id"]: c.get("parent_id") for c in client.list_collections()}
    assert by_id == {"root": None, 1: None, 2: 1, 3: 2, 4: 1}
