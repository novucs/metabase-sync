from typing import Any

from pydantic import ValidationError

from metabase_sync.errors import ValidationFailed, describe
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.collection import Collection
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.format.representations.snippet import Snippet
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.ids import swap_ids

type Item = Collection | Card | Dashboard | Snippet | Subscription

COLLECTION = "collection"
SNIPPET = "snippet"
CARD = "card"
DASHBOARD = "dashboard"
SUBSCRIPTION = "subscription"
KINDS: dict[str, type[Item]] = {
    COLLECTION: Collection,
    SNIPPET: Snippet,
    CARD: Card,
    DASHBOARD: Dashboard,
    SUBSCRIPTION: Subscription,
}
MODELS: dict[str, str] = {
    "Collection": COLLECTION,
    "NativeQuerySnippet": SNIPPET,
    "Card": CARD,
    "Dashboard": DASHBOARD,
    "Pulse": SUBSCRIPTION,
}
SERDES_MODELS = {KINDS[kind]: model for model, kind in MODELS.items()}
CONTAINERS: dict[str, dict[str, str]] = {
    COLLECTION: {"parent_id": COLLECTION},
    SNIPPET: {},
    CARD: {"collection_id": COLLECTION, "dashboard_id": DASHBOARD},
    DASHBOARD: {"collection_id": COLLECTION},
    SUBSCRIPTION: {"dashboard_id": DASHBOARD},
}
RECORDED = frozenset({"creator_id", "database_id", "disable_links"})
COMPARED_WHEN_PRESENT = frozenset({"result_metadata"})


def kind_of(data: dict[str, Any]) -> str | None:
    meta = data.get("serdes/meta")
    last = meta[-1] if isinstance(meta, list) and meta else None
    model = last.get("model") if isinstance(last, dict) else None
    return MODELS.get(model) if isinstance(model, str) else None


def read(data: dict[str, Any], kind: str, where: object) -> Item:
    fields = {k: v for k, v in data.items() if k != "serdes/meta"}
    try:
        return KINDS[kind].model_validate(fields, strict=True, extra="forbid")
    except ValidationError as error:
        problems = describe(error, KINDS[kind])
        raise ValidationFailed([f"{where}: {p}" for p in problems]) from None


def write(item: Item) -> dict[str, Any]:
    return {
        **item.model_dump(exclude_defaults=True),
        "serdes/meta": [{"model": SERDES_MODELS[type(item)]}],
    }


def comparable(item: Item) -> dict[str, Any]:
    return {k: v for k, v in write(item).items() if k not in RECORDED}


def swapped(item: Item, mapping: dict[str, str]) -> Item:
    data = item.model_dump()
    renamed = swap_ids(data, mapping)
    return item if renamed == data else type(item).model_validate(renamed)


def collection_of(item: Item) -> str | None:
    if isinstance(item, Collection):
        return item.parent_id
    if isinstance(item, Card | Dashboard):
        return item.collection_id
    return None
