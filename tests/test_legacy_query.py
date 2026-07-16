"""Card serialization round-trips.

For native cards: the SQL body is byte-faithful via the native `query` slot.
For GUI cards: we accept either Metabase representation Schema —
  - classic `{database, type:'query', query:{...}}` (older Metabase)
  - MBQL5 `{lib/type, database, stages:[...]}` (newer Metabase)
and round-trip whichever shape the server gave us. The diff path strips
`lib/uuid` so MBQL5 stays stable across saves.
"""

import json
from pathlib import Path

import yaml

from metabase_sync.models import Card
from metabase_sync.serialize.cards import read_card_file, write_card

NATIVE_LEGACY = json.dumps(
    {
        "database": 2,
        "type": "native",
        "native": {
            "query": "SELECT * FROM t WHERE x = {{x}}",
            "template-tags": {
                "x": {
                    "type": "text",
                    "name": "x",
                    "id": "fixed-uuid",
                    "display-name": "X",
                }
            },
        },
    }
)


def _make_native_card(**overrides) -> Card:
    base = {
        "id": 1,
        "entity_id": "ENT1",
        "name": "My Native Card",
        "type": "question",
        "display": "table",
        "database_id": 2,
        "collection_id": 9,
        "dashboard_id": None,
        "parameters": [],
        "visualization_settings": {},
        "legacy_query": NATIVE_LEGACY,
        "dataset_query": {},
    }
    base.update(overrides)
    return Card.model_validate(base)


def test_native_card_from_dataset_query_only_round_trip(tmp_path: Path):
    """On older Metabase versions (e.g. v0.55) and for cards we POSTed
    ourselves, the server may not populate `legacy_query` at all — the native
    info is only in `dataset_query`. `_is_native` and `write_card` must agree
    on the extension so the file round-trips.
    """
    base = {
        "id": 1,
        "entity_id": "ENT1",
        "name": "no-legacy-native",
        "type": "question",
        "display": "table",
        "database_id": 2,
        "collection_id": 9,
        "dashboard_id": None,
        "parameters": [],
        "visualization_settings": {},
        "legacy_query": None,
        "dataset_query": {
            "database": 2,
            "type": "native",
            "native": {
                "query": "SELECT 1 AS one",
                "template-tags": {},
            },
        },
    }
    card = Card.model_validate(base)
    path = tmp_path / "card.sql"
    write_card(path, card, db_name_by_id={2: "sample"})

    # Must have written valid frontmatter (the v0.55 breakage was a .sql file
    # holding a YAML doc).
    assert path.read_text().startswith("---\n")
    fm, body, gui = read_card_file(path)
    assert body == "SELECT 1 AS one"
    assert gui is None
    assert fm["database"] == "sample"


def test_native_card_from_mbql5_stage_round_trip(tmp_path: Path):
    """Modern Metabase exposes some native cards as an MBQL5 stage. We still
    write them as `.sql` so users can edit the SQL directly."""
    base = {
        "id": 1,
        "entity_id": "ENT1",
        "name": "stages-native",
        "type": "question",
        "display": "table",
        "database_id": 2,
        "collection_id": 9,
        "dashboard_id": None,
        "parameters": [],
        "visualization_settings": {},
        "legacy_query": None,
        "dataset_query": {
            "lib/type": "mbql/query",
            "database": 2,
            "stages": [{"lib/type": "mbql.stage/native", "native": "SELECT 99"}],
        },
    }
    card = Card.model_validate(base)
    path = tmp_path / "card.sql"
    write_card(path, card, db_name_by_id={2: "sample"})

    fm, body, gui = read_card_file(path)
    assert body == "SELECT 99"
    assert gui is None
    assert fm["database"] == "sample"


def test_export_prefers_live_dataset_query_over_stale_legacy_query(tmp_path: Path):
    """The `legacy_query` projection can lag behind the live `dataset_query`
    after an API write: Metabase answers a PUT by updating dataset_query while
    legacy_query still holds the pre-write SQL. The diff path already trusts the
    live query (test_card_diff.test_live_dataset_query_wins_over_stale_legacy_query);
    export must use the same precedence, or it writes the stale SQL back to disk
    and a correctly-applied change reads as though it never landed.
    """
    card = _make_native_card(
        legacy_query=json.dumps(
            {
                "database": 2,
                "type": "native",
                "native": {"query": "SELECT stale", "template-tags": {}},
            }
        ),
        dataset_query={
            "database": 2,
            "type": "native",
            "native": {"query": "SELECT fresh", "template-tags": {}},
        },
    )
    path = tmp_path / "card.sql"
    write_card(path, card, db_name_by_id={2: "bigquery"})

    _fm, body, _gui = read_card_file(path)
    assert body == "SELECT fresh"


def test_native_card_round_trip(tmp_path: Path):
    card = _make_native_card()
    path = tmp_path / "card.sql"
    write_card(path, card, db_name_by_id={2: "bigquery"})

    fm, body, gui = read_card_file(path)
    assert body == "SELECT * FROM t WHERE x = {{x}}"
    assert gui is None
    assert fm["entity_id"] == "ENT1"
    assert fm["database"] == "bigquery"
    assert fm["template_tags"]["x"]["id"] == "fixed-uuid"


def test_gui_card_round_trip_classic_form(tmp_path: Path):
    legacy = json.dumps(
        {
            "database": 2,
            "type": "query",
            "query": {"source-table": 42, "aggregation": [["count"]]},
        }
    )
    card = _make_native_card(name="GUI", legacy_query=legacy, display="bar")
    path = tmp_path / "card.yaml"
    write_card(path, card, db_name_by_id={2: "bigquery"})
    fm, body, gui = read_card_file(path)
    assert body is None
    assert gui == {
        "database": 2,
        "type": "query",
        "query": {"source-table": 42, "aggregation": [["count"]]},
    }
    assert fm["database"] == "bigquery"


def test_gui_card_round_trip_mbql5_form(tmp_path: Path):
    legacy = json.dumps(
        {
            "lib/type": "mbql/query",
            "database": 2,
            "stages": [{"lib/type": "mbql.stage/mbql", "source-table": 2}],
        }
    )
    card = _make_native_card(name="GUI", legacy_query=legacy, display="bar")
    path = tmp_path / "card.yaml"
    write_card(path, card, db_name_by_id={2: "bigquery"})
    fm, body, gui = read_card_file(path)
    assert body is None
    assert gui == {
        "lib/type": "mbql/query",
        "database": 2,
        "stages": [{"lib/type": "mbql.stage/mbql", "source-table": 2}],
    }


def test_export_strips_volatile_lib_uuid(tmp_path: Path):
    """Metabase regenerates lib/uuid on every save; persisting them makes
    every export rewrite otherwise-unchanged cards. Written files must carry
    none, in the GUI dataset_query or in native template_tags dimensions."""
    legacy = json.dumps(
        {
            "lib/type": "mbql/query",
            "database": 2,
            "stages": [
                {
                    "lib/type": "mbql.stage/mbql",
                    "lib/uuid": "aaaa-1111",
                    "source-table": 2,
                }
            ],
        }
    )
    gui_card = _make_native_card(name="GUI", legacy_query=legacy, display="bar")
    gui_path = tmp_path / "card.yaml"
    write_card(gui_path, gui_card, db_name_by_id={2: "bigquery"})
    _fm, _body, gui = read_card_file(gui_path)
    assert "lib/uuid" not in gui_path.read_text()
    assert gui["stages"][0]["lib/type"] == "mbql.stage/mbql"

    native_legacy = json.dumps(
        {
            "type": "native",
            "database": 2,
            "native": {
                "query": "SELECT {{d}}",
                "template-tags": {
                    "d": {
                        "type": "dimension",
                        "name": "d",
                        "dimension": [
                            "field",
                            {"lib/uuid": "bbbb-2222", "base-type": "type/Date"},
                            8589,
                        ],
                    }
                },
            },
        }
    )
    native_card = _make_native_card(legacy_query=native_legacy)
    native_path = tmp_path / "card.sql"
    write_card(native_path, native_card, db_name_by_id={2: "bigquery"})
    assert "lib/uuid" not in native_path.read_text()
    fm, body, _gui = read_card_file(native_path)
    assert body == "SELECT {{d}}"
    assert fm["template_tags"]["d"]["dimension"][2] == 8589


def test_gui_card_backwards_compat_old_on_disk_format(tmp_path: Path):
    """Files written by an earlier pre-release of the tool stored only the
    classic `query` sub-dict at top level. read_card_file lifts them into the
    full dataset_query shape so existing trees keep loading."""
    path = tmp_path / "card.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "entity_id": "old",
                "name": "Old GUI Card",
                "display": "bar",
                "query": {"source-table": 7, "aggregation": [["count"]]},
            }
        )
    )
    fm, body, gui = read_card_file(path)
    assert body is None
    assert gui == {
        "type": "query",
        "query": {"source-table": 7, "aggregation": [["count"]]},
    }
