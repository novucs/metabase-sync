from pathlib import Path

import pytest

from metabase_sync.errors import ConfigError, UnresolvedReferenceError
from metabase_sync.format import representations
from metabase_sync.format.api import v58
from metabase_sync.format.representations import CARD, DASHBOARD
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.dashboard import Dashboard, Series
from metabase_sync.format.representations.subscription import Channel, Subscription
from metabase_sync.metabase.index import Index, RemoteItem
from metabase_sync.metabase.metadata import Metadata
from metabase_sync.metabase.remote import Remote
from metabase_sync.settings import load_settings
from metabase_sync.store import tree, yamlio
from metabase_sync.store.state import STATE
from metabase_sync.store.tree import read_tree
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.validate import validate


@pytest.fixture
def no_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("URL", "API_KEY", "HEADERS"):
        monkeypatch.delenv(f"METABASE_{name}", raising=False)
    return tmp_path


def test_missing_settings_are_named_by_their_variable(no_settings):
    with pytest.raises(ConfigError) as raised:
        load_settings()

    assert "METABASE_URL is not set" in str(raised.value)


def test_settings_are_read_from_a_dotenv_file_in_the_current_directory(no_settings):
    (no_settings / ".env").write_text(
        "METABASE_URL=http://mb.test\nMETABASE_API_KEY=mb_test\nOTHER=1\n"
    )

    assert load_settings().url == "http://mb.test"


def test_malformed_headers_are_reported_as_configuration(monkeypatch):
    monkeypatch.setenv("METABASE_URL", "http://mb.test")
    monkeypatch.setenv("METABASE_HEADERS", "not json")

    with pytest.raises(ConfigError) as raised:
        load_settings()

    assert "METABASE_HEADERS" in str(raised.value)


def test_a_misspelt_config_key_is_rejected(tmp_path):
    yamlio.write(tmp_path / tree.CONFIG, {"colections": ["A" * 21]})

    with pytest.raises(ConfigError) as raised:
        workflow.load_config(tmp_path)

    assert "colections" in str(raised.value)


def test_schema_problems_name_the_file_and_the_dashcard(tmp_path):
    yamlio.write(tmp_path / tree.CONFIG, {})
    yamlio.write(
        tmp_path / "collections" / "main" / "board.yaml",
        {
            "name": "Board",
            "entity_id": "B" * 21,
            "dashcards": [
                {
                    "entity_id": "C" * 21,
                    "row": 4,
                    "col": 2,
                    "size_x": "wide",
                    "size_y": 2,
                }
            ],
            "serdes/meta": [{"model": "Dashboard"}],
        },
    )

    errors = validate(read_tree(tmp_path)).errors

    board = Path("collections/main/board.yaml")
    assert errors == [f"{board}: dashcards.0.size_x: Input should be a valid integer"]


def test_a_misspelt_field_inside_a_dashcard_is_rejected(tmp_path):
    yamlio.write(tmp_path / tree.CONFIG, {})
    yamlio.write(
        tmp_path / "collections" / "main" / "board.yaml",
        {
            "name": "Board",
            "entity_id": "B" * 21,
            "dashcards": [
                {
                    "entity_id": "C" * 21,
                    "row": 0,
                    "col": 0,
                    "size_x": 4,
                    "size_y": 2,
                    "visualisation_settings": {},
                }
            ],
            "serdes/meta": [{"model": "Dashboard"}],
        },
    )

    errors = validate(read_tree(tmp_path)).errors

    board = Path("collections/main/board.yaml")
    assert errors == [
        f"{board}: dashcards.0.visualisation_settings: Extra inputs are not permitted"
    ]


def test_a_file_whose_collection_was_removed_is_reported(tmp_path):
    collection = "C" * 21
    yamlio.write(tmp_path / tree.CONFIG, {})
    (tmp_path / STATE).write_text(f"collection {collection} abc\n")
    yamlio.write(
        tmp_path / "collections" / "main" / "orphan.yaml",
        {
            "name": "Orphan",
            "entity_id": "Q" * 21,
            "collection_id": collection,
            "display": "table",
            "dataset_query": {},
            "visualization_settings": {},
            "serdes/meta": [{"model": "Card"}],
        },
    )

    errors = validate(read_tree(tmp_path)).errors

    orphan = Path("collections/main/orphan.yaml")
    assert errors == [
        f"{orphan}: collection_id {collection} was removed from the files"
    ]


def index() -> Index:
    found = Index(
        version="v0.65.3",
        metadata=None,  # type: ignore[arg-type]
        databases={1: "Warehouse"},
        users={7: "admin@example.com"},
    )
    found.add(DASHBOARD, {"id": 3, "entity_id": "D" * 21, "name": "Board"})
    found.add(CARD, {"id": 5, "entity_id": "Q" * 21})
    return found


def test_a_dashcard_series_from_metabase_reads_as_card_entity_ids():
    raw = {
        "id": 3,
        "entity_id": "D" * 21,
        "name": "Board",
        "dashcards": [
            {
                "id": 9,
                "entity_id": "E" * 21,
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
                "series": [{"id": 5, "name": "Q"}],
            }
        ],
    }

    dashboard = Remote(index=index(), api=v58).read(DASHBOARD, raw)

    assert isinstance(dashboard, Dashboard)
    assert dashboard.dashcards[0].series == [Series(card_id="Q" * 21, position=0)]


def test_a_cleared_description_is_sent_as_null_and_unset_fields_are_left_out():
    card = Card(
        name="Q",
        entity_id="Q" * 21,
        type="model",
        display="table",
        dataset_query={"database": "Warehouse"},
        visualization_settings={},
    )

    sent = v58.card.request(card, index()).model_dump(mode="json")

    assert sent["description"] is None
    assert "result_metadata" not in sent


def test_recipients_are_sent_as_a_user_id_or_an_email_never_both():
    subscription = Subscription(
        entity_id="S" * 21,
        dashboard_id="D" * 21,
        channels=[
            Channel(
                channel_type="email",
                recipients=["admin@example.com", "someone@example.com"],
            )
        ],
    )

    sent = v58.subscription.request(
        subscription, index(), RemoteItem(id=3, name="Board")
    ).model_dump(mode="json")

    assert [c["recipients"] for c in sent["channels"]] == [
        [{"id": 7}, {"email": "someone@example.com"}]
    ]


def test_a_file_reads_back_exactly_as_written():
    original = {
        "name": "Q",
        "entity_id": "Q" * 21,
        "display": "table",
        "collection_id": "C" * 21,
        "dataset_query": {"database": "Warehouse", "lib/type": "mbql/query"},
        "visualization_settings": {},
        "serdes/meta": [{"model": "Card"}],
    }

    assert (
        representations.write(representations.read(original, CARD, "q.yaml"))
        == original
    )


class DeactivatedField:
    def get(self, path: str, **params: object) -> dict[str, object]:
        return {
            "/api/field/7": {"table_id": 3},
            "/api/table/3": {"db_id": 1},
            "/api/database/1/metadata": {
                "name": "Warehouse",
                "tables": [{"id": 3, "name": "ORDERS", "fields": []}],
            },
        }[path]


def test_a_field_missing_from_the_database_metadata_is_unresolved_not_a_crash():
    metadata = Metadata(DeactivatedField(), {1: "Warehouse"})  # type: ignore[arg-type]

    with pytest.raises(UnresolvedReferenceError):
        metadata.field_ref(7)


def test_a_model_column_targeting_a_deactivated_field_keeps_its_id_with_a_warning():
    found = index()
    found.metadata = Metadata(DeactivatedField(), {1: "Warehouse"})  # type: ignore[arg-type]
    raw = {
        "id": 5,
        "entity_id": "Q" * 21,
        "name": "Model",
        "type": "model",
        "display": "table",
        "dataset_query": {},
        "result_metadata": [{"name": "PRODUCT_ID", "fk_target_field_id": 7}],
    }

    card = Remote(index=found, api=v58).read(CARD, raw)

    assert isinstance(card, Card) and card.result_metadata is not None
    assert card.result_metadata[0].fk_target_field_id == 7
    assert found.warnings
