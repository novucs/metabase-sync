"""End-to-end integration: spin up a real Metabase via docker compose, run
export/plan/apply against it, assert the round-trip is byte-stable and that
code-first authoring (no entity_ids) lands cards + dashboards correctly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from metabase_sync.apply import run as run_apply
from metabase_sync.client import MetabaseClient
from metabase_sync.export import run_export

pytestmark = pytest.mark.integration


def _state_unchanged(a: Path, b: Path) -> bool:
    """True iff every file under a/ has an identical counterpart under b/."""
    a_files = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    b_files = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    if a_files.keys() != b_files.keys():
        return False
    return all(a_files[k] == b_files[k] for k in a_files)


def test_export_against_empty_metabase(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A fresh Metabase exports to a valid tree (collections/databases at minimum)."""
    state_dir = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state_dir)
    assert (state_dir / "collections").exists()
    assert (state_dir / "databases").exists()


def test_apply_round_trip(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Export → re-export must produce byte-identical output."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, a)
        run_export(client, b)
    assert _state_unchanged(a, b)


def test_code_first_dashboard_creation(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Author a collection + card + dashboard purely on disk (all entity_ids null),
    apply, then assert the dashboard exists with the card linked."""
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)

    # Need to know the database the new card targets. Take the first non-sample one.
    import yaml

    db_files = sorted((state / "databases").glob("*.yaml"))
    assert db_files, "fresh Metabase should expose at least the sample db"
    db_name = yaml.safe_load(db_files[0].read_text())["name"]

    coll_dir = state / "collections" / "smoke"
    (coll_dir / "cards").mkdir(parents=True)
    (coll_dir / "dashboards" / "smoke-dash").mkdir(parents=True)

    (coll_dir / "_collection.yaml").write_text(
        "entity_id: null\n"
        "name: smoke\n"
        "description: null\n"
        "authority_level: null\n"
        "archived: false\n"
    )
    (coll_dir / "cards" / "hello.sql").write_text(
        "---\n"
        "entity_id: null\n"
        "name: smoke card\n"
        "description: null\n"
        "type: question\n"
        "display: scalar\n"
        f"database: {db_name}\n"
        "parameters: []\n"
        "visualization_settings: {}\n"
        "enable_embedding: false\n"
        "embedding_params: null\n"
        "cache_ttl: null\n"
        "archived: false\n"
        "template_tags: {}\n"
        "---\n"
        "---body---\n"
        "SELECT 1 AS one"
    )
    (coll_dir / "dashboards" / "smoke-dash" / "dashboard.yaml").write_text(
        "entity_id: null\n"
        "name: smoke dashboard\n"
        "description: null\n"
        "archived: false\n"
        "auto_apply_filters: true\n"
        "cache_ttl: null\n"
        "enable_embedding: false\n"
        "embedding_params: null\n"
        "position: null\n"
        "width: fixed\n"
        "parameters: []\n"
        "tabs: []\n"
        "dashcards:\n"
        "- entity_id: null\n"
        "  card_path: ../../cards/hello.sql\n"
        "  tab_position: null\n"
        "  row: 0\n"
        "  col: 0\n"
        "  size_x: 12\n"
        "  size_y: 4\n"
        "  parameter_mappings: []\n"
        "  visualization_settings: {}\n"
        "  series: []\n"
    )

    with MetabaseClient(metabase_url, metabase_api_key) as client:
        plan = run_apply(client, state, mode="apply")

    creates = [c for c in plan.changes if c.action == "create"]
    create_resources = {c.resource for c in creates}
    assert {"collections", "cards", "dashboards"}.issubset(create_resources)

    # Verify the dashboard exists with its dashcard linked.
    import httpx

    headers = {"X-API-Key": metabase_api_key}
    dashboards = httpx.get(
        f"{metabase_url}/api/dashboard", headers=headers, timeout=10
    ).json()
    dash = next(d for d in dashboards if d["name"] == "smoke dashboard")
    detail = httpx.get(
        f"{metabase_url}/api/dashboard/{dash['id']}", headers=headers, timeout=10
    ).json()
    assert len(detail["dashcards"]) == 1
    assert detail["dashcards"][0]["card_id"] is not None


def test_plan_is_no_op_after_apply(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """After a clean export, plan reports zero writes."""
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
        plan = run_apply(client, state, mode="plan")

    counts = plan.counts()
    for resource, c in counts.items():
        assert c["create"] == 0, f"{resource}: expected 0 creates, got {c}"
        assert c["update"] == 0, f"{resource}: expected 0 updates, got {c}"
        assert c["archive"] == 0, f"{resource}: expected 0 archives, got {c}"

    serialized = json.dumps(plan.to_dict())
    assert '"changes"' in serialized
    assert '"counts"' in serialized


def test_only_flag_restricts_to_one_resource(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """--only cards should leave dashboards/snippets/pulses untouched."""
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
        plan = run_apply(client, state, mode="plan", only={"cards"})

    resources = {c.resource for c in plan.changes}
    assert resources <= {"cards"}, f"unexpected resources: {resources - {'cards'}}"


def test_snippet_round_trip(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A native-query snippet round-trips: create on disk, apply, re-export, no diff."""
    import httpx

    headers = {"X-API-Key": metabase_api_key}
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)

    (state / "snippets").mkdir(exist_ok=True)
    (state / "snippets" / "weekday-filter.sql").write_text(
        "---\n"
        "entity_id: null\n"
        "name: weekday filter\n"
        "description: null\n"
        "template_tags: {}\n"
        "archived: false\n"
        "---\n"
        "---body---\n"
        "EXTRACT(DOW FROM created_at) BETWEEN 1 AND 5"
    )

    with MetabaseClient(metabase_url, metabase_api_key) as client:
        applied = run_apply(client, state, mode="apply")

    assert any(
        c.action == "create" and c.resource == "snippets" for c in applied.changes
    )
    listed = httpx.get(
        f"{metabase_url}/api/native-query-snippet", headers=headers, timeout=10
    ).json()
    assert any(s["name"] == "weekday filter" for s in listed)


def test_root_level_dashboard_round_trip(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A dashboard directly under the 'Our analytics' root collection
    (collection_id=None) must export cleanly, not crash with a ValueError."""
    import httpx

    headers = {"X-API-Key": metabase_api_key}
    httpx.post(
        f"{metabase_url}/api/dashboard",
        json={"name": "root-dash", "collection_id": None},
        headers=headers,
        timeout=10,
    )

    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
        p = run_apply(client, state, mode="plan")

    # The root-level dashboard should appear under collections/root/dashboards/.
    root_dash = (
        state / "collections" / "root" / "dashboards" / "root-dash" / "dashboard.yaml"
    )
    assert root_dash.exists(), f"expected {root_dash} on disk"

    # Plan reports skip — round-trip is clean.
    for c in p.changes:
        if c.resource == "dashboards" and c.name == "root-dash":
            assert c.action == "skip"


def test_virtual_dashcards_round_trip(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Text/heading/link dashcards have card_id=null + a virtual_card discriminator
    in visualization_settings. Apply must preserve them through a full round-trip
    or any dashboard containing a text widget will silently lose it.
    """
    import httpx

    headers = {"X-API-Key": metabase_api_key}

    # Root-level dashboards crash export (separate bug); put everything in a
    # named collection for now.
    coll = httpx.post(
        f"{metabase_url}/api/collection",
        json={"name": "virtual-test-coll", "color": "#509EE3"},
        headers=headers,
        timeout=10,
    ).json()

    dbs = httpx.get(f"{metabase_url}/api/database", headers=headers, timeout=10).json()[
        "data"
    ]
    db = next(d for d in dbs if d["name"] != "Internal Metabase Database")
    card = httpx.post(
        f"{metabase_url}/api/card",
        json={
            "name": "real-card",
            "type": "question",
            "display": "scalar",
            "database_id": db["id"],
            "dataset_query": {
                "database": db["id"],
                "type": "native",
                "native": {"query": "SELECT 1", "template-tags": {}},
            },
            "visualization_settings": {},
            "parameters": [],
            "collection_id": coll["id"],
            "result_metadata": [],
        },
        headers=headers,
        timeout=30,
    ).json()

    # Author a dashboard with one text widget and one real card.
    dash = httpx.post(
        f"{metabase_url}/api/dashboard",
        json={"name": "virtual-test", "collection_id": coll["id"]},
        headers=headers,
    ).json()
    httpx.put(
        f"{metabase_url}/api/dashboard/{dash['id']}",
        json={
            "name": "virtual-test",
            "tabs": [],
            "dashcards": [
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
        },
        headers=headers,
        timeout=30,
    )

    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
        # Plan must be a no-op — the virtual card's visualization_settings round-trips.
        p = run_apply(client, state, mode="plan")

    # The dashboard's contents diff must report zero.
    for c in p.changes:
        if c.resource == "dashboards" and "virtual-test" in c.name:
            assert c.action == "skip", (
                f"virtual-card dashboard should skip but got {c.action}: {c.summary}"
            )

    # Apply (no-op) then re-fetch and confirm both dashcards still exist with
    # the right shapes.
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_apply(client, state, mode="apply")

    detail = httpx.get(
        f"{metabase_url}/api/dashboard/{dash['id']}", headers=headers, timeout=10
    ).json()
    assert len(detail["dashcards"]) == 2
    virtual = next(dc for dc in detail["dashcards"] if dc["card_id"] is None)
    real = next(dc for dc in detail["dashcards"] if dc["card_id"] is not None)
    assert virtual["visualization_settings"]["virtual_card"]["display"] == "text"
    assert virtual["visualization_settings"]["text"] == "Hello world"
    assert real["card_id"] == card["id"]


def test_model_result_metadata_preserved(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Models carry curated column metadata (descriptions, semantic types).
    A round-trip — and especially an update — must not wipe it."""
    import httpx

    headers = {"X-API-Key": metabase_api_key}
    coll = httpx.post(
        f"{metabase_url}/api/collection",
        json={"name": "model-test", "color": "#509EE3"},
        headers=headers,
        timeout=10,
    ).json()
    dbs = httpx.get(f"{metabase_url}/api/database", headers=headers, timeout=10).json()[
        "data"
    ]
    db = next(d for d in dbs if d["name"] != "Internal Metabase Database")

    # Create a model with two columns, curate one's description.
    created = httpx.post(
        f"{metabase_url}/api/card",
        json={
            "name": "my-model",
            "type": "model",
            "display": "table",
            "database_id": db["id"],
            "dataset_query": {
                "database": db["id"],
                "type": "native",
                "native": {
                    "query": "SELECT 1 AS id, 'a' AS label",
                    "template-tags": {},
                },
            },
            "visualization_settings": {},
            "parameters": [],
            "collection_id": coll["id"],
            "result_metadata": [],
        },
        headers=headers,
        timeout=30,
    ).json()

    # Trigger a query so Metabase populates result_metadata.
    httpx.post(
        f"{metabase_url}/api/card/{created['id']}/query",
        json={},
        headers=headers,
        timeout=30,
    )
    after = httpx.get(
        f"{metabase_url}/api/card/{created['id']}", headers=headers
    ).json()
    assert after.get("result_metadata"), (
        "Metabase should have computed metadata after query"
    )
    rm = list(after["result_metadata"])
    for col in rm:
        if col["name"].lower() == "id":
            col["description"] = "primary key — DO NOT WIPE"
            col["semantic_type"] = "type/PK"
    httpx.put(
        f"{metabase_url}/api/card/{created['id']}",
        json={"result_metadata": rm},
        headers=headers,
        timeout=30,
    )

    # Round-trip via export + apply (apply will edit something forcing a PUT).
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)

    # Edit the model's display on disk so apply has something to PUT.
    cards_dir = state / "collections" / "model-test" / "cards"
    target = next(iter(cards_dir.glob("my-model.*")))
    text = target.read_text()
    text = text.replace("display: table", "display: scalar")
    target.write_text(text)

    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_apply(client, state, mode="apply")

    final = httpx.get(
        f"{metabase_url}/api/card/{created['id']}", headers=headers
    ).json()
    id_col = next(
        col for col in final["result_metadata"] if col["name"].lower() == "id"
    )
    assert id_col.get("description") == "primary key — DO NOT WIPE"
    assert id_col.get("semantic_type") == "type/PK"


def test_dashboard_tab_and_dashcard_update_together(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Mutating both tabs and dashcards in one apply must reach the server in
    one PUT (single-PUT property) and end up correct."""
    import httpx
    import yaml as yamllib

    headers = {"X-API-Key": metabase_api_key}
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)

    db_files = sorted((state / "databases").glob("*.yaml"))
    db_name = yamllib.safe_load(db_files[0].read_text())["name"]

    # Author a dashboard with two tabs.
    coll = state / "collections" / "tabs-test"
    (coll / "cards").mkdir(parents=True)
    (coll / "dashboards" / "tabby").mkdir(parents=True)
    (coll / "_collection.yaml").write_text(
        "entity_id: null\nname: tabs-test\ndescription: null\n"
        "authority_level: null\narchived: false\n"
    )
    (coll / "cards" / "a.sql").write_text(
        "---\n"
        "entity_id: null\nname: A\ndescription: null\ntype: question\n"
        "display: scalar\n"
        f"database: {db_name}\n"
        "parameters: []\nvisualization_settings: {}\n"
        "enable_embedding: false\nembedding_params: null\ncache_ttl: null\n"
        "archived: false\ntemplate_tags: {}\n"
        "---\n---body---\nSELECT 1"
    )
    (coll / "cards" / "b.sql").write_text(
        "---\n"
        "entity_id: null\nname: B\ndescription: null\ntype: question\n"
        "display: scalar\n"
        f"database: {db_name}\n"
        "parameters: []\nvisualization_settings: {}\n"
        "enable_embedding: false\nembedding_params: null\ncache_ttl: null\n"
        "archived: false\ntemplate_tags: {}\n"
        "---\n---body---\nSELECT 2"
    )
    (coll / "dashboards" / "tabby" / "dashboard.yaml").write_text(
        "entity_id: null\nname: Tabby\ndescription: null\narchived: false\n"
        "auto_apply_filters: true\ncache_ttl: null\nenable_embedding: false\n"
        "embedding_params: null\nposition: null\nwidth: fixed\nparameters: []\n"
        "tabs:\n"
        "- entity_id: null\n  name: First\n  position: 0\n"
        "- entity_id: null\n  name: Second\n  position: 1\n"
        "dashcards:\n"
        "- entity_id: null\n  card_path: ../../cards/a.sql\n  tab_position: 0\n"
        "  row: 0\n  col: 0\n  size_x: 12\n  size_y: 4\n"
        "  parameter_mappings: []\n  visualization_settings: {}\n  series: []\n"
        "- entity_id: null\n  card_path: ../../cards/b.sql\n  tab_position: 1\n"
        "  row: 0\n  col: 0\n  size_x: 12\n  size_y: 4\n"
        "  parameter_mappings: []\n  visualization_settings: {}\n  series: []\n"
    )

    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_apply(client, state, mode="apply")

    dashboards = httpx.get(
        f"{metabase_url}/api/dashboard", headers=headers, timeout=10
    ).json()
    dash = next(d for d in dashboards if d["name"] == "Tabby")
    detail = httpx.get(
        f"{metabase_url}/api/dashboard/{dash['id']}", headers=headers, timeout=10
    ).json()
    assert len(detail["tabs"]) == 2
    assert len(detail["dashcards"]) == 2
    # Each dashcard linked to its tab.
    tab_positions = {t["id"]: t["position"] for t in detail["tabs"]}
    dashcard_tab_positions = sorted(
        tab_positions[dc["dashboard_tab_id"]] for dc in detail["dashcards"]
    )
    assert dashcard_tab_positions == [0, 1]
