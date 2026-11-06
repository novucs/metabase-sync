from pathlib import Path

import pytest

from metabase_sync.errors import (
    ConfigError,
    StateError,
    UnresolvedReferenceError,
    ValidationFailed,
)
from metabase_sync.format import representations
from metabase_sync.format.api import v58
from metabase_sync.format.representations import (
    CARD,
    DASHBOARD,
    SNIPPET,
    SUBSCRIPTION,
)
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.dashboard import Dashboard, Series
from metabase_sync.format.representations.subscription import Channel, Subscription
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.metabase.index import Index, RemoteItem, fetch_index
from metabase_sync.metabase.metadata import Metadata
from metabase_sync.metabase.remote import Remote
from metabase_sync.settings import Settings, load_settings
from metabase_sync.store import tree, yamlio
from metabase_sync.store.state import STATE, Managed
from metabase_sync.store.tree import Entity, LocalTree, read_tree
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.operations import local_hash
from metabase_sync.sync.plan import build_plan
from metabase_sync.sync.validate import validate


@pytest.fixture
def no_settings(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("URL", "API_KEY", "HEADERS"):
        monkeypatch.delenv(f"METABASE_{name}", raising=False)
    return tmp_path


def test_missing_settings_are_named_by_their_variable(no_settings):
    with pytest.raises(ConfigError) as raised:
        load_settings(no_settings)

    assert "METABASE_URL is not set" in str(raised.value)


def test_settings_are_read_from_a_dotenv_file_in_the_project_directory(no_settings):
    project = no_settings / "project"
    project.mkdir()
    (project / ".env").write_text(
        "METABASE_URL=http://mb.test\nMETABASE_API_KEY=mb_test\nOTHER=1\n"
    )

    assert load_settings(project).base == "http://mb.test"


def test_malformed_headers_are_reported_as_configuration(no_settings, monkeypatch):
    monkeypatch.setenv("METABASE_URL", "http://mb.test")
    monkeypatch.setenv("METABASE_HEADERS", "not json")

    with pytest.raises(ConfigError) as raised:
        load_settings(no_settings)

    assert "METABASE_HEADERS" in str(raised.value)


def test_a_misspelt_config_key_is_rejected_with_the_closest_key(tmp_path):
    yamlio.write(tmp_path / tree.CONFIG, {"colections": ["A" * 21]})

    with pytest.raises(ConfigError) as raised:
        workflow.load_config(tmp_path)

    assert "colections: unknown field; did you mean collections?" in str(raised.value)


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


def test_a_dashcard_without_a_tab_on_a_dashboard_with_tabs_is_warned_about(tmp_path):
    yamlio.write(tmp_path / tree.CONFIG, {})
    yamlio.write(
        tmp_path / "collections" / "main" / "board.yaml",
        {
            "name": "Board",
            "entity_id": "B" * 21,
            "tabs": [{"entity_id": "T" * 21, "name": "One", "position": 0}],
            "dashcards": [
                {"entity_id": "C" * 21, "row": 4, "col": 2, "size_x": 1, "size_y": 1}
            ],
            "serdes/meta": [{"model": "Dashboard"}],
        },
    )

    report = validate(read_tree(tmp_path))

    assert report.warnings == [
        "collections/main/board.yaml: the dashcard at row 4, column 2 has no "
        + "dashboard_tab_id, so it is on none of the dashboard's tabs"
    ]


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
        f"{board}: dashcards.0.visualisation_settings: unknown field; did you mean "
        + "visualization_settings?"
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
        f"{orphan}: collection_id {collection} was removed from the files; restore "
        + "its file, or point collection_id at another collection"
    ]


SETTINGS = Settings.model_validate({"url": "http://mb.test", "api_key": "k"})


class DeactivatedField(MetabaseClient):
    def __init__(self) -> None:
        super().__init__(SETTINGS)
        self.requested: list[str] = []

    def get(self, path: str, **params: str) -> dict[str, object]:
        self.requested.append(path)
        return {
            "/api/field/7": {"table_id": 3},
            "/api/table/3": {"db_id": 1},
            "/api/database/1/metadata": {
                "name": "Warehouse",
                "tables": [{"id": 3, "name": "ORDERS", "fields": []}],
            },
        }[path]


class TwoWarehouses(MetabaseClient):
    def __init__(self) -> None:
        super().__init__(SETTINGS)

    def get(self, path: str, **params: str) -> object:
        if path == "/api/session/properties":
            return {"version": {"tag": "v0.65.3"}}
        if path == "/api/database":
            return {
                "data": [{"id": 1, "name": "Warehouse"}, {"id": 2, "name": "Warehouse"}]
            }
        return []


def test_a_file_using_a_database_name_two_databases_share_is_refused():
    found = fetch_index(TwoWarehouses())
    card = Card(
        name="Q",
        entity_id="Q" * 21,
        display="table",
        dataset_query={"database": "Warehouse"},
        visualization_settings={},
    )
    local = LocalTree(root=Path("."))
    local.entities[card.entity_id] = Entity(kind=CARD, data=card, path=Path("q.yaml"))

    with pytest.raises(ValidationFailed) as raised:
        build_plan(local, Remote(index=found, api=v58))

    assert raised.value.problems == [
        "q.yaml: dataset_query refers to database 'Warehouse', but 2 databases in "
        + "Metabase have that name; rename one of them in Metabase"
    ]


def test_a_misspelt_table_in_a_new_question_is_refused_before_applying():
    databases = {"Warehouse": [1]}
    found = Index(
        version="v0.65.3",
        metadata=Metadata(DeactivatedField(), databases),
        databases=databases,
    )
    card = Card(
        name="Q",
        entity_id="N" * 21,
        display="table",
        dataset_query={
            "database": "Warehouse",
            "stages": [{"source-table": ["Warehouse", None, "ORDRS"]}],
        },
        visualization_settings={},
    )
    local = LocalTree(root=Path("."))
    local.entities[card.entity_id] = Entity(kind=CARD, data=card, path=Path("q.yaml"))

    with pytest.raises(ValidationFailed) as raised:
        build_plan(local, Remote(index=found, api=v58))

    assert raised.value.problems == [
        "q.yaml: dataset_query: table ['Warehouse', None, 'ORDRS'] not found"
    ]


def test_a_link_to_a_deleted_database_is_kept_with_a_warning():
    found = index()
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
                "visualization_settings": {
                    "link": {"entity": {"id": 2, "model": "database"}}
                },
            }
        ],
    }

    dashboard = Remote(index=found, api=v58).read(DASHBOARD, raw)

    assert isinstance(dashboard, Dashboard)
    link = dashboard.dashcards[0].visualization_settings["link"]
    assert link["entity"]["id"] == 2
    assert found.missing


def test_an_unreadable_reference_names_the_item_it_is_in():
    raw = {
        "id": 3,
        "entity_id": "D" * 21,
        "name": "Board",
        "dashcards": [
            {
                "id": 9,
                "entity_id": "E" * 21,
                "card_id": 99,
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
            }
        ],
    }

    with pytest.raises(StateError) as raised:
        Remote(index=index(), api=v58).read(DASHBOARD, raw)

    assert "dashboard 3 'Board'" in str(raised.value)
    assert "card id 99" in str(raised.value)


def test_a_card_on_a_database_metabase_does_not_list_reads_with_a_warning():
    found = index()
    raw = {
        "id": 6,
        "entity_id": "A" * 21,
        "name": "Audit",
        "display": "table",
        "database_id": 13371337,
        "dataset_query": {"database": 13371337},
    }

    card = Remote(index=found, api=v58).read(CARD, raw)

    assert isinstance(card, Card) and card.database_id is None
    assert found.missing == [
        "card 'Audit' refers to database 13371337, which Metabase doesn't list"
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a, \nFROM t\n",
        "SELECT\n\ta\nFROM t",
        "SELECT a\nFROM t  ",
        "\tSELECT a\nFROM t",
    ],
)
def test_sql_with_tabs_or_trailing_spaces_is_written_as_a_block(sql: str):
    text = yamlio.dumps({"native": sql})

    assert text.startswith("native: |")
    assert yamlio.loads(text) == {"native": sql}


@pytest.mark.parametrize(
    "sql", ["SELECT a\n  \nFROM t", "\tSELECT a\n\nFROM t\n", " SELECT\n\tb \n\t\n"]
)
def test_whitespace_only_lines_survive_a_round_trip(sql: str):
    assert yamlio.loads(yamlio.dumps({"native": sql})) == {"native": sql}


def test_an_untabbed_dashcard_on_a_dashboard_with_tabs_reads():
    found = index()
    raw = {
        "id": 3,
        "entity_id": "D" * 21,
        "name": "Board",
        "tabs": [{"id": 4, "entity_id": "T" * 21, "name": "One", "position": 0}],
        "dashcards": [
            {
                "id": 9,
                "entity_id": "E" * 21,
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
            }
        ],
    }
    found.add(DASHBOARD, raw)

    dashboard = Remote(index=found, api=v58).read(DASHBOARD, raw)

    assert isinstance(dashboard, Dashboard) and dashboard.dashcards[0].tab is None


def test_an_action_button_is_reported_as_not_managed():
    found = index()
    raw = {
        "id": 3,
        "entity_id": "D" * 21,
        "name": "Board",
        "dashcards": [
            {
                "id": 9,
                "entity_id": "E" * 21,
                "action_id": 5,
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
            }
        ],
    }

    Remote(index=found, api=v58).read(DASHBOARD, raw)

    assert any("'Board'" in w and "action" in w for w in found.warnings)


class SubscriptionWithoutChannels(MetabaseClient):
    def __init__(self) -> None:
        super().__init__(SETTINGS)

    def get(self, path: str, **params: str) -> object:
        board = {"id": 3, "entity_id": "D" * 21, "name": "Board"}
        live = {"id": 8, "entity_id": "S" * 21, "dashboard_id": 3, "channels": []}
        archived = {**live, "id": 9, "entity_id": "R" * 21, "archived": True}
        elsewhere = {**live, "id": 10, "entity_id": "O" * 21, "dashboard_id": 4}
        return {
            "/api/session/properties": {"version": {"tag": "v0.65.3"}},
            "/api/dashboard": [board] if params.get("f") == "all" else [],
            "/api/dashboard/3": board,
            "/api/pulse": [archived] if params.get("archived") else [live, elsewhere],
        }.get(path, [])


def test_a_subscription_without_channels_is_skipped_with_a_warning():
    found = fetch_index(SubscriptionWithoutChannels())

    pulled = workflow.remote_entities(Remote(index=found, api=v58))

    assert [e.kind for e in pulled] == [DASHBOARD]
    assert [w for w in found.warnings if "subscription" in w] == [
        "skipped subscription 8 on dashboard 3: it has no channels, so the files "
        + "can't hold it"
    ]


def test_a_managed_subscription_that_lost_its_channels_is_never_recreated():
    found = fetch_index(SubscriptionWithoutChannels())
    board = Dashboard(name="Board", entity_id="D" * 21)
    subscription = Subscription(
        entity_id="S" * 21,
        dashboard_id="D" * 21,
        channels=[Channel(channel_type="email", recipients=["a@example.com"])],
    )
    local = LocalTree(root=Path("."))
    for kind, item in ((DASHBOARD, board), (SUBSCRIPTION, subscription)):
        entity = Entity(kind=kind, data=item, path=Path(f"{kind}.yaml"))
        local.entities[item.entity_id] = entity
        local.state[item.entity_id] = Managed(kind=kind, hash=local_hash(entity))

    plan = build_plan(local, Remote(index=found, api=v58), force=True)

    assert [op.action for op in plan.operations if op.entity_id == "S" * 21] == []
    assert [c.entity_id for c in plan.conflicts] == ["S" * 21]


def test_a_snippet_with_no_content_reads():
    raw = {"id": 4, "entity_id": "N" * 21, "name": "Empty", "content": ""}

    snippet = Remote(index=index(), api=v58).read(SNIPPET, raw)

    assert representations.write(snippet)["content"] == ""


def index(client: MetabaseClient | None = None) -> Index:
    databases = {"Warehouse": [1]}
    found = Index(
        version="v0.65.3",
        metadata=Metadata(client or MetabaseClient(SETTINGS), databases),
        databases=databases,
        users={7: "admin@example.com"},
    )
    found.add(DASHBOARD, {"id": 3, "entity_id": "D" * 21, "name": "Board"})
    found.add(CARD, {"id": 5, "entity_id": "Q" * 21})
    return found


def test_a_subscription_card_without_a_dashcard_is_left_out():
    raw = {
        "id": 8,
        "entity_id": "S" * 21,
        "dashboard_id": 3,
        "channels": [{"channel_type": "email", "recipients": []}],
        "cards": [{"dashboard_card_id": None, "include_csv": True}],
    }

    subscription = Remote(index=index(), api=v58).read(SUBSCRIPTION, raw)

    assert isinstance(subscription, Subscription)
    assert subscription.attachments == []


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


def test_a_missing_field_is_looked_up_in_metabase_only_once():
    client = DeactivatedField()
    metadata = Metadata(client, {"Warehouse": [1]})

    for _ in range(2):
        with pytest.raises(UnresolvedReferenceError):
            metadata.field_ref(7)

    assert client.requested == [
        "/api/field/7",
        "/api/table/3",
        "/api/database/1/metadata",
    ]


def test_a_field_missing_from_the_databases_in_use_needs_no_lookup():
    client = DeactivatedField()
    metadata = Metadata(client, {"Warehouse": [1]})
    metadata.in_scope = {1}

    for _ in range(2):
        with pytest.raises(UnresolvedReferenceError):
            metadata.field_ref(7)

    assert client.requested == ["/api/database/1/metadata"]


def test_a_question_using_a_field_gone_from_its_database_says_so_without_lookups():
    client = DeactivatedField()
    found = index(client)
    found.metadata.in_scope = {1}
    raw = {
        "id": 5,
        "entity_id": "Q" * 21,
        "name": "Model",
        "type": "model",
        "display": "table",
        "database_id": 1,
        "dataset_query": {"database": 1},
        "result_metadata": [{"name": "PRODUCT_ID", "fk_target_field_id": 7}],
    }

    Remote(index=found, api=v58).read(CARD, raw)

    assert "/api/field/7" not in client.requested
    assert found.broken == [
        "card 'Model' uses field 7, which no longer exists in the database"
    ]


def test_a_model_column_targeting_a_deactivated_field_keeps_its_id_with_a_warning():
    found = index(DeactivatedField())
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
    assert found.broken
