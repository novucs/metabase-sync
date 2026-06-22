"""Dashboard integration tests: code-first creation, root-level, virtual
dashcards, tabs+dashcards together, parameters/mappings, nested collections."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metabase_sync.apply import run as run_apply
from metabase_sync.client import MetabaseClient
from metabase_sync.export import run_export

from ._builders import (
    dashcard_to,
    unique_name,
    write_collection,
    write_dashboard,
    write_native_card,
)
from ._mb import MetabaseAdmin

pytestmark = pytest.mark.integration


def _apply(url: str, key: str, state: Path, **kw):
    with MetabaseClient(url, key) as client:
        return run_apply(client, state, **kw)


def _export(url: str, key: str, state: Path) -> None:
    with MetabaseClient(url, key) as client:
        run_export(client, state)


def test_code_first_dashboard_creation(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    db = mb.sample_db_name()
    dash_name = unique_name("smoke-dash")
    state = tmp_path / "state"
    write_collection(state, "smoke", unique_name("smoke"))
    write_native_card(
        state, "smoke", "hello", unique_name("hello"), "SELECT 1 AS one", db
    )
    write_dashboard(
        state, "smoke", "smoke-dash", dash_name, dashcards=[dashcard_to("hello.sql")]
    )

    plan = _apply(metabase_url, metabase_api_key, state, mode="apply")
    assert {"collections", "cards", "dashboards"} <= {
        c.resource for c in plan.changes if c.action == "create"
    }

    detail = mb.get_dashboard(mb.find_dashboard(dash_name)["id"])
    assert len(detail["dashcards"]) == 1
    assert detail["dashcards"][0]["card_id"] is not None


def test_root_level_dashboard(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A dashboard under the root collection (collection_id=None) exports under
    collections/root/dashboards and round-trips clean."""
    name = unique_name("root-dash")
    mb.create_dashboard(name, collection_id=None)

    state = tmp_path / "state"
    _export(metabase_url, metabase_api_key, state)
    p = _apply(metabase_url, metabase_api_key, state, mode="plan")

    assert (state / "collections" / "root" / "dashboards").exists()
    for c in p.changes:
        if c.resource == "dashboards" and c.name == name:
            assert c.action == "skip"


def test_virtual_dashcards_round_trip(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Text widgets (card_id=null + virtual_card discriminator) survive a
    round-trip."""
    coll = mb.create_collection(unique_name("virtual-coll"))
    card = mb.create_native_card(
        unique_name("real"), "SELECT 1", collection_id=coll["id"]
    )
    dash = mb.create_dashboard(unique_name("virtual-dash"), collection_id=coll["id"])
    mb.set_dashboard_contents(
        dash["id"],
        [
            {
                "id": -1,
                "card_id": None,
                "row": 0,
                "col": 0,
                "size_x": 12,
                "size_y": 2,
                "parameter_mappings": [],
                "visualization_settings": {
                    "virtual_card": {
                        "name": None,
                        "display": "text",
                        "visualization_settings": {},
                        "dataset_query": {},
                        "archived": False,
                    },
                    "text": "Hello world",
                },
                "series": [],
            },
            {
                "id": -2,
                "card_id": card["id"],
                "row": 2,
                "col": 0,
                "size_x": 12,
                "size_y": 4,
                "parameter_mappings": [],
                "visualization_settings": {},
                "series": [],
            },
        ],
    )

    state = tmp_path / "state"
    _export(metabase_url, metabase_api_key, state)
    p = _apply(metabase_url, metabase_api_key, state, mode="plan")
    for c in p.changes:
        if c.resource == "dashboards" and c.name == dash["name"]:
            assert c.action == "skip", f"virtual dashboard should skip: {c.summary}"

    _apply(metabase_url, metabase_api_key, state, mode="apply")
    detail = mb.get_dashboard(dash["id"])
    assert len(detail["dashcards"]) == 2
    virtual = next(dc for dc in detail["dashcards"] if dc["card_id"] is None)
    assert virtual["visualization_settings"]["virtual_card"]["display"] == "text"
    assert virtual["visualization_settings"]["text"] == "Hello world"


def test_tabs_and_dashcards_together(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Mutating tabs + dashcards in one apply lands correctly (single-PUT)."""
    db = mb.sample_db_name()
    dash_name = unique_name("tabby")
    state = tmp_path / "state"
    write_collection(state, "tabs", unique_name("tabs"))
    write_native_card(state, "tabs", "a", unique_name("A"), "SELECT 1", db)
    write_native_card(state, "tabs", "b", unique_name("B"), "SELECT 2", db)
    write_dashboard(
        state,
        "tabs",
        "tabby",
        dash_name,
        tabs=[{"name": "First", "position": 0}, {"name": "Second", "position": 1}],
        dashcards=[
            dashcard_to("a.sql", tab_position=0),
            dashcard_to("b.sql", tab_position=1),
        ],
    )

    _apply(metabase_url, metabase_api_key, state, mode="apply")

    detail = mb.get_dashboard(mb.find_dashboard(dash_name)["id"])
    assert len(detail["tabs"]) == 2
    assert len(detail["dashcards"]) == 2
    tab_pos = {t["id"]: t["position"] for t in detail["tabs"]}
    assert sorted(tab_pos[dc["dashboard_tab_id"]] for dc in detail["dashcards"]) == [
        0,
        1,
    ]


def test_dashboard_parameters_and_mappings_round_trip(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A dashboard filter wired to a card via parameter_mappings must round-trip
    (export → plan no-op). Built server-side because parameter_mappings embed the
    real card id."""
    coll = mb.create_collection(unique_name("param-coll"))
    tags = {
        "cat": {
            "type": "text",
            "name": "cat",
            "id": "22222222-2222-2222-2222-222222222222",
            "display-name": "Cat",
        }
    }
    card = mb.create_native_card(
        unique_name("filtered"),
        "SELECT {{cat}} AS v",
        collection_id=coll["id"],
        template_tags=tags,
    )
    dash = mb.create_dashboard(unique_name("param-dash"), collection_id=coll["id"])
    mb.put(
        f"/api/dashboard/{dash['id']}",
        {
            "parameters": [
                {"id": "p1", "name": "Cat", "slug": "cat", "type": "category"}
            ],
            "tabs": [],
            "dashcards": [
                {
                    "id": -1,
                    "card_id": card["id"],
                    "row": 0,
                    "col": 0,
                    "size_x": 12,
                    "size_y": 4,
                    "series": [],
                    "visualization_settings": {},
                    "parameter_mappings": [
                        {
                            "parameter_id": "p1",
                            "card_id": card["id"],
                            "target": ["variable", ["template-tag", "cat"]],
                        }
                    ],
                }
            ],
        },
    )

    state = tmp_path / "state"
    _export(metabase_url, metabase_api_key, state)

    dash_yaml = next(
        p for p in state.rglob("dashboard.yaml") if dash["name"] in p.read_text()
    )
    doc = yaml.safe_load(dash_yaml.read_text())
    assert doc["parameters"] and doc["parameters"][0]["slug"] == "cat"
    assert doc["dashcards"][0]["parameter_mappings"][0]["parameter_id"] == "p1"

    p = _apply(metabase_url, metabase_api_key, state, mode="plan")
    for c in p.changes:
        if c.resource == "dashboards" and c.name == dash["name"]:
            assert c.action == "skip", f"param dashboard should round-trip: {c.summary}"


def test_nested_collections_depth_2(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """collection → sub-collection → card resolves and the server reflects the
    parent/child relationship."""
    db = mb.sample_db_name()
    parent_name = unique_name("parent")
    child_name = unique_name("child")
    card_name = unique_name("deep-card")
    state = tmp_path / "state"
    write_collection(state, "parent", parent_name)
    write_collection(state, "parent/child", child_name)
    write_native_card(state, "parent/child", "deep", card_name, "SELECT 1", db)

    _apply(metabase_url, metabase_api_key, state, mode="apply")

    colls = mb.get("/api/collection")
    parent = next(c for c in colls if c.get("name") == parent_name)
    child = next(c for c in colls if c.get("name") == child_name)
    # `parent_id` is absent from the list on some versions (v0.55 uses
    # `location`); derive parentage robustly.
    assert _parent_id(child) == parent["id"]
    assert mb.find_card(card_name)["collection_id"] == child["id"]


def _parent_id(coll: dict) -> int | None:
    if coll.get("parent_id") is not None:
        return coll["parent_id"]
    parts = [p for p in (coll.get("location") or "").split("/") if p]
    return int(parts[-1]) if parts else None
