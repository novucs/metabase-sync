"""Card integration tests: native update, GUI/MBQL create+update, parameterized
native, model metadata preservation, move between collections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metabase_sync.apply import run as run_apply
from metabase_sync.client import MetabaseClient
from metabase_sync.export import run_export

from ._builders import unique_name, write_collection, write_gui_card, write_native_card
from ._mb import MetabaseAdmin

pytestmark = pytest.mark.integration


def _apply(url: str, key: str, state: Path, **kw):
    with MetabaseClient(url, key) as client:
        return run_apply(client, state, **kw)


def test_native_card_sql_update_lands(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Edit a native card's SQL on disk; apply; the server must receive the new
    SQL, and a follow-up plan must be a no-op."""
    db = mb.sample_db_name()
    name = unique_name("upd-card")
    state = tmp_path / "state"
    write_collection(state, "c", "C")
    write_native_card(state, "c", "card", name, "SELECT 1 AS a", db)

    _apply(metabase_url, metabase_api_key, state, mode="apply")

    # Edit the SQL on disk and re-apply.
    card_file = state / "collections" / "c" / "cards" / "card.sql"
    card_file.write_text(
        card_file.read_text().replace("SELECT 1 AS a", "SELECT 2 AS b")
    )
    _apply(metabase_url, metabase_api_key, state, mode="apply")

    detail = mb.find_card(name)
    assert "SELECT 2 AS b" in json.dumps(detail)

    plan = _apply(metabase_url, metabase_api_key, state, mode="plan")
    assert all(c.action == "skip" for c in plan.changes if c.name == name)


def test_gui_card_create_and_update(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Author a GUI/MBQL card from disk, apply, then change its display and
    apply again. Exercises the dataset_query path that only round-trips as a
    no-op against sample data today."""
    db = mb.sample_db_name()
    table_id = mb.sample_table_id()
    name = unique_name("gui-card")
    dataset_query = {
        "database": mb.sample_db_id(),
        "type": "query",
        "query": {"source-table": table_id, "limit": 1},
    }
    state = tmp_path / "state"
    write_collection(state, "g", "G")
    write_gui_card(state, "g", "card", name, dataset_query, db, display="table")

    _apply(metabase_url, metabase_api_key, state, mode="apply")
    created = mb.find_card(name)
    assert created["display"] == "table"
    assert created["query_type"] == "query"

    card_file = state / "collections" / "g" / "cards" / "card.yaml"
    card_file.write_text(
        card_file.read_text().replace("display: table", "display: bar")
    )
    _apply(metabase_url, metabase_api_key, state, mode="apply")
    assert mb.find_card(name)["display"] == "bar"


def test_parameterized_native_card(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A native card with a {{var}} template-tag applies and the tag survives."""
    db = mb.sample_db_name()
    name = unique_name("param-card")
    tags = {
        "x": {
            "type": "text",
            "name": "x",
            "id": "11111111-1111-1111-1111-111111111111",
            "display-name": "X",
        }
    }
    state = tmp_path / "state"
    write_collection(state, "p", "P")
    write_native_card(
        state, "p", "card", name, "SELECT {{x}} AS v", db, template_tags=tags
    )

    _apply(metabase_url, metabase_api_key, state, mode="apply")
    detail = mb.find_card(name)
    assert "{{x}}" in json.dumps(detail)
    # Round-trip must converge (no spurious diff on the template tag).
    plan = _apply(metabase_url, metabase_api_key, state, mode="plan")
    assert all(c.action == "skip" for c in plan.changes if c.name == name)


def test_model_result_metadata_preserved(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A model's curated column metadata must survive a disk-driven update."""
    coll = mb.create_collection(unique_name("model-coll"))
    name = unique_name("my-model")
    created = mb.create_model(
        name, "SELECT 1 AS id, 'a' AS label", collection_id=coll["id"]
    )
    mb.run_card_query(created["id"])  # populate result_metadata

    detail = mb.get_card(created["id"])
    assert detail.get("result_metadata"), "model should have metadata after a query"
    rm = list(detail["result_metadata"])
    for col in rm:
        if col["name"].lower() == "id":
            col["description"] = "primary key — DO NOT WIPE"
            col["semantic_type"] = "type/PK"
    mb.update_card(created["id"], {"result_metadata": rm})

    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)

    # Force a PUT by editing the model's display on disk.
    coll_dir = next(
        p.parent
        for p in state.rglob("_collection.yaml")
        if coll["name"] in p.read_text()
    )
    target = next((coll_dir / "cards").glob("*"))
    target.write_text(target.read_text().replace("display: table", "display: scalar"))

    _apply(metabase_url, metabase_api_key, state, mode="apply")

    final = mb.get_card(created["id"])
    id_col = next(c for c in final["result_metadata"] if c["name"].lower() == "id")
    assert id_col.get("description") == "primary key — DO NOT WIPE"
    assert id_col.get("semantic_type") == "type/PK"


def test_card_moved_between_collections(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Relocating a card's file to another collection on disk moves it on the
    server (collection_id changes)."""
    db = mb.sample_db_name()
    name = unique_name("movable")
    state = tmp_path / "state"
    write_collection(state, "src", unique_name("src-coll"))
    dst_name = unique_name("dst-coll")
    write_collection(state, "dst", dst_name)
    src_file = write_native_card(state, "src", "card", name, "SELECT 1", db)

    _apply(metabase_url, metabase_api_key, state, mode="apply")

    # Move the file (carrying its written-back entity_id) into dst.
    dst_file = state / "collections" / "dst" / "cards" / "card.sql"
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    dst_file.write_text(src_file.read_text())
    src_file.unlink()

    _apply(metabase_url, metabase_api_key, state, mode="apply")

    moved = mb.find_card(name)
    dst_coll = next(c for c in mb.get("/api/collection") if c.get("name") == dst_name)
    assert moved["collection_id"] == dst_coll["id"]
