import io
import tarfile
from types import ModuleType
from typing import Any

import pytest
import requests
import yaml

from metabase_sync.format.api import VERSIONS
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    KINDS,
    SNIPPET,
    SUBSCRIPTION,
    Item,
)
from metabase_sync.format.representations.card import Card, ResultColumn
from metabase_sync.format.representations.collection import Collection
from metabase_sync.format.representations.dashboard import (
    Dashboard,
    Dashcard,
    Series,
    Tab,
)
from metabase_sync.format.representations.snippet import Snippet
from metabase_sync.format.representations.subscription import (
    Attachment,
    Channel,
    Subscription,
)
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.metabase.index import Index
from metabase_sync.metabase.metadata import Metadata
from metabase_sync.settings import Settings

from ..pins import REPRESENTATIONS

pytestmark = pytest.mark.contracts

REMOVAL = "removal is managed by removing the file"
RECORDED = "recorded by Metabase, not authored"
PUBLIC_LINK = "public links are created per instance, not managed"
GETTING_STARTED = "legacy field Metabase's UI no longer edits"
LEFT_OUT: dict[type, dict[str, str]] = {
    Collection: {
        "archived": REMOVAL,
        "archived_directly": REMOVAL,
        "archive_operation_id": REMOVAL,
        "created_at": RECORDED,
        "slug": "derived from the name",
        "is_sample": RECORDED,
        "is_remote_synced": RECORDED,
        "namespace": "only regular collections are managed",
        "type": "only regular collections are managed",
        "personal_owner_id": "personal collections are not managed",
    },
    Card: {
        "archived": REMOVAL,
        "archived_directly": REMOVAL,
        "created_at": RECORDED,
        "metabase_version": RECORDED,
        "card_schema": RECORDED,
        "query_type": "derived from dataset_query",
        "public_uuid": PUBLIC_LINK,
        "made_public_by_id": PUBLIC_LINK,
        "document_id": "cards inside documents are not managed",
    },
    Snippet: {
        "archived": REMOVAL,
        "created_at": RECORDED,
        "template_tags": "derived from content",
        "collection_id": "snippet folders need a paid plan and are not managed",
    },
    Dashboard: {
        "archived": REMOVAL,
        "archived_directly": REMOVAL,
        "created_at": RECORDED,
        "initially_published_at": RECORDED,
        "public_uuid": PUBLIC_LINK,
        "made_public_by_id": PUBLIC_LINK,
        "caveats": GETTING_STARTED,
        "points_of_interest": GETTING_STARTED,
        "show_in_getting_started": GETTING_STARTED,
        "position": GETTING_STARTED,
    },
    Dashcard: {"created_at": RECORDED},
}

COVERAGE = [
    ("collection.yaml", ("properties",), Collection),
    ("card.yaml", ("properties",), Card),
    ("snippet.yaml", ("properties",), Snippet),
    ("dashboard.yaml", ("properties",), Dashboard),
    ("dashboard.yaml", ("properties", "tabs", "items", "properties"), Tab),
    ("dashboard.yaml", ("$defs", "dashcard", "properties"), Dashcard),
    ("dashboard.yaml", ("$defs", "series", "properties"), Series),
]


@pytest.fixture(scope="module")
def representations() -> dict[str, Any]:
    url = (
        "https://registry.npmjs.org/@metabase/representations/-/"
        f"representations-{REPRESENTATIONS}.tgz"
    )
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    schemas = {}
    with tarfile.open(fileobj=io.BytesIO(response.content)) as archive:
        for member in archive.getmembers():
            if member.name.startswith("package/core-spec/v1/schemas/"):
                name = member.name.rsplit("/", 1)[-1]
                extracted = archive.extractfile(member)
                if extracted is not None and name.endswith(".yaml"):
                    schemas.setdefault(name, yaml.safe_load(extracted.read()))
    return schemas


@pytest.mark.parametrize(
    ("file", "path", "model"), COVERAGE, ids=[m.__name__ for _, _, m in COVERAGE]
)
def test_the_files_cover_every_field_metabase_defines(
    representations, file, path, model
):
    schema = representations[file]
    for key in path:
        schema = schema[key]
    theirs = set(schema) - {"serdes/meta"}
    ours = set(model.model_fields)
    left_out = LEFT_OUT.get(model, {})

    assert ours - theirs == set(), "fields Metabase's format doesn't define"
    assert theirs - ours == set(left_out), "fields neither managed nor left out"


@pytest.fixture(scope="module", params=VERSIONS, ids=[f"0.{v}" for v in VERSIONS])
def version(request: pytest.FixtureRequest) -> tuple[ModuleType, dict[str, Any]]:
    response = requests.get(
        f"https://www.metabase.com/docs/v0.{request.param}/api.json", timeout=60
    )
    response.raise_for_status()
    return VERSIONS[request.param], response.json()


def _resolve(api: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = api["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _fields(api: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    schema = _resolve(api, schema)
    for variant in schema.get("oneOf", []) + schema.get("anyOf", []):
        if found := _fields(api, variant):
            return found
    if schema.get("type") == "array":
        return _fields(api, schema.get("items", {}))
    return schema.get("properties", {})


def _accepted(api: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    for key in (path, path.replace("/{id}", "") + "/" if "{id}" not in path else path):
        operation = api["paths"].get(key, {}).get(method)
        if operation:
            body = operation["requestBody"]["content"]["application/json"]["schema"]
            return _fields(api, body)
    raise AssertionError(f"{method.upper()} {path} is not in Metabase's API")


NESTED = ("tabs", "dashcards", "series", "channels", "cards", "recipients")


def _unaccepted(api: dict[str, Any], body: Any, fields: dict[str, Any]) -> list[str]:
    found = [key for key in body if key not in fields]
    for key in NESTED:
        if key in body and key in fields:
            inner = _fields(api, fields[key])
            for item in body[key] if inner else []:
                found += [f"{key}.{k}" for k in _unaccepted(api, item, inner)]
    return found


DASHBOARD_ID = "D" * 21
DASHCARD = "E" * 21
TAB = "T" * 21
CARD_ID = "Q" * 21
ITEMS: dict[str, Item] = {
    COLLECTION: Collection(name="C", entity_id="C" * 21, parent_id="P" * 21),
    CARD: Card(
        name="Q",
        entity_id=CARD_ID,
        collection_id="C" * 21,
        dashboard_id=DASHBOARD_ID,
        type="model",
        display="table",
        dataset_query={},
        visualization_settings={},
        result_metadata=[ResultColumn(name="ID")],
    ),
    DASHBOARD: Dashboard(
        name="D",
        entity_id=DASHBOARD_ID,
        tabs=[Tab(entity_id=TAB, name="One", position=0)],
        dashcards=[
            Dashcard(
                entity_id=DASHCARD,
                dashboard_tab_id=[DASHBOARD_ID, TAB],
                card_id=CARD_ID,
                row=0,
                col=0,
                size_x=4,
                size_y=4,
                series=[Series(card_id=CARD_ID, position=0)],
            )
        ],
    ),
    SNIPPET: Snippet(name="S", entity_id="S" * 21, content="1 = 1"),
    SUBSCRIPTION: Subscription(
        entity_id="P" * 21,
        dashboard_id=DASHBOARD_ID,
        channels=[
            Channel(
                channel_type="email",
                schedule_type="daily",
                recipients=["user@example.com", "guest@example.com"],
            )
        ],
        attachments=[Attachment(dashcard_id=DASHCARD, include_csv=True)],
    ),
}
CREATE, UPDATE, CONTENTS = "create", "update", "contents"
ENDPOINTS = [(kind, e) for kind in KINDS for e in (CREATE, UPDATE)] + [
    (DASHBOARD, CONTENTS)
]


def _index() -> Index:
    databases = {"Warehouse": [1]}
    settings = Settings.model_validate({"url": "http://mb.test", "api_key": "k"})
    index = Index(
        version="v0.65.3",
        metadata=Metadata(MetabaseClient(settings), databases),
        databases=databases,
        users={1: "user@example.com"},
    )
    index.add(COLLECTION, {"id": 1, "entity_id": "C" * 21})
    index.add(COLLECTION, {"id": 9, "entity_id": "P" * 21})
    index.add(CARD, {"id": 1, "entity_id": CARD_ID})
    index.add(
        DASHBOARD,
        {
            "id": 2,
            "entity_id": DASHBOARD_ID,
            "name": "D",
            "tabs": [{"id": 5, "entity_id": TAB}],
            "dashcards": [{"id": 3, "entity_id": DASHCARD, "card_id": 1}],
        },
    )
    return index


def _body(resource: ModuleType, kind: str, endpoint: str) -> dict[str, Any]:
    item, index = ITEMS[kind], _index()
    if endpoint == CONTENTS:
        current = index.items[DASHBOARD][2]
        return resource.contents(item, index, current).model_dump(mode="json")
    if isinstance(item, Subscription):
        request = resource.request(item, index, index.items[DASHBOARD][2])
    else:
        request = resource.request(item, index)
    fixed = set() if endpoint == CREATE else resource.FIXED
    return request.model_dump(mode="json", exclude=fixed)


@pytest.mark.parametrize(
    ("kind", "endpoint"), ENDPOINTS, ids=[f"{k}-{e}" for k, e in ENDPOINTS]
)
def test_metabase_accepts_every_field_we_send(version, kind, endpoint):
    package, api = version
    resource = package.RESOURCES[kind]
    put = _accepted(api, resource.ENDPOINT + "/{id}", "put")
    accepted = (
        put
        if endpoint != CREATE
        else {**put, **_accepted(api, resource.ENDPOINT, "post")}
    )

    assert _unaccepted(api, _body(resource, kind, endpoint), accepted) == []
