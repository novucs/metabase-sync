from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, field_validator

from metabase_sync.errors import UnresolvedReferenceError
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    SNIPPET,
    SUBSCRIPTION,
)
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.metabase.metadata import Metadata


def location_ids(location: str | None) -> list[int]:
    return [int(p) for p in (location or "/").strip("/").split("/") if p]


class RemoteTab(BaseModel):
    id: int
    entity_id: str | None = None
    name: str = ""
    position: int = 0


class RemoteDashcard(BaseModel):
    id: int
    entity_id: str | None = None
    card_id: int | None = None
    dashboard_tab_id: int | None = None
    row: int = 0
    col: int = 0


class RemoteItem(BaseModel):
    id: int
    entity_id: str | None = None
    name: str = ""
    archived: bool | None = None
    archived_directly: bool | None = None
    collection_id: int | None = None
    location: str | None = None
    dashboard_id: int | None = None
    document_id: int | None = None
    updated_at: str | None = None
    personal_owner_id: int | None = None
    namespace: str | None = None
    type: str | None = None
    is_personal: bool | None = None
    tabs: list[RemoteTab] = []
    dashcards: list[RemoteDashcard] = []

    @field_validator("name", mode="before")
    @classmethod
    def unnamed(cls, name: str | None) -> str:
        return name or ""

    @property
    def parent_id(self) -> int | None:
        ids = location_ids(self.location)
        return ids[-1] if ids else None

    def collection(self, kind: str) -> int | None:
        return self.parent_id if kind == COLLECTION else self.collection_id

    def container(self, kind: str) -> int | None:
        return self.dashboard_id if kind == SUBSCRIPTION else self.collection(kind)

    @property
    def regular_collection(self) -> bool:
        return (
            self.personal_owner_id is None
            and self.namespace is None
            and self.type is None
            and not self.is_personal
        )


def rows(listing: Any) -> list[dict[str, Any]]:
    found = listing["data"] if isinstance(listing, dict) else listing
    return [row for row in found if isinstance(row.get("id"), int)]


@dataclass
class Index:
    version: str
    metadata: Metadata
    minor: int = 0
    items: dict[str, dict[int, RemoteItem]] = field(default_factory=dict)
    raw: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    outside: dict[tuple[str, int], str] = field(default_factory=dict)
    databases: dict[int, str] = field(default_factory=dict)
    users: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    _by_eid: dict[str, tuple[str, int]] = field(default_factory=dict)
    _dashcards: dict[int, str] = field(default_factory=dict)
    _tabs: dict[int, list[str]] = field(default_factory=dict)

    def add(self, kind: str, raw: dict[str, Any]) -> RemoteItem:
        item = RemoteItem.model_validate(raw, extra="ignore")
        self.items.setdefault(kind, {})[item.id] = item
        self.raw[kind, item.id] = raw
        if item.entity_id:
            self._by_eid[item.entity_id] = (kind, item.id)
        self.learn_dashboard(item)
        return item

    def forget(self, kind: str, item_id: int) -> None:
        item = self.items.get(kind, {}).pop(item_id, None)
        self.raw.pop((kind, item_id), None)
        if item is not None and item.entity_id:
            self._by_eid.pop(item.entity_id, None)

    def learn_dashboard(self, item: RemoteItem) -> None:
        for dc in item.dashcards:
            if dc.entity_id:
                self._dashcards[dc.id] = dc.entity_id
        for tab in item.tabs:
            if tab.entity_id and item.entity_id:
                self._tabs[tab.id] = [item.entity_id, tab.entity_id]

    def get(self, kind: str, item_id: int | None) -> RemoteItem | None:
        return self.items.get(kind, {}).get(item_id) if item_id is not None else None

    def find(self, entity_id: str) -> tuple[str, RemoteItem] | None:
        kind, item_id = self._by_eid.get(entity_id, (None, None))
        item = self.get(kind, item_id) if kind else None
        return (kind, item) if kind and item else None

    def all(self, kind: str) -> list[RemoteItem]:
        return list(self.items.get(kind, {}).values())

    def live(self, kind: str) -> list[RemoteItem]:
        return [i for i in self.all(kind) if not i.archived]

    def entity_id(self, kind: str, item_id: int) -> str:
        item = self.get(kind, item_id)
        found = item.entity_id if item else self.outside.get((kind, item_id))
        if not found:
            raise UnresolvedReferenceError(
                f"{kind} id {item_id} is not visible to metabase-sync"
            )
        return found

    def add_outside(self, kind: str, item_id: int, entity_id: str) -> None:
        self.outside[kind, item_id] = entity_id
        self._by_eid.setdefault(entity_id, (kind, item_id))

    def existing_id(self, kind: str, entity_id: str) -> int | None:
        found = self._by_eid.get(entity_id)
        return found[1] if found is not None and found[0] == kind else None

    def id(self, kind: str, entity_id: str) -> int:
        found = self.existing_id(kind, entity_id)
        if found is None:
            raise UnresolvedReferenceError(
                f"{kind} {entity_id} does not exist in Metabase"
            )
        return found

    def tab_pair(self, tab_id: int) -> list[str]:
        if tab_id not in self._tabs:
            raise UnresolvedReferenceError(f"dashboard tab id {tab_id} not found")
        return self._tabs[tab_id]

    def dashcard_entity_id(self, dashcard_id: int) -> str:
        if dashcard_id not in self._dashcards:
            raise UnresolvedReferenceError(f"dashcard id {dashcard_id} not found")
        return self._dashcards[dashcard_id]

    def database_name(self, database_id: int) -> str:
        if database_id not in self.databases:
            raise UnresolvedReferenceError(f"database id {database_id} not found")
        return self.databases[database_id]

    def database_id(self, name: str) -> int:
        for database_id, database in self.databases.items():
            if database == name:
                return database_id
        raise UnresolvedReferenceError(
            f"database {name!r} not found in Metabase",
            hint="Database names must match the Metabase admin database list",
        )

    def user_id(self, email: str) -> int | None:
        for user_id, user in self.users.items():
            if user.lower() == email.lower():
                return user_id
        return None

    def lenient(self, call: Any, value: Any, what: str) -> Any:
        try:
            return call(value)
        except UnresolvedReferenceError as error:
            self.warnings.append(f"kept numeric {what} {value}: {error}")
            return value


def with_detail(summary: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    if detail.get("archived"):
        return {**summary, **detail, "collection_id": summary.get("collection_id")}
    return {**summary, **detail}


def fetch_index(client: MetabaseClient, roots: set[str] | None = None) -> Index:
    raw = client.map(
        lambda args: client.get(args[0], **args[1]),
        [
            (
                "/api/collection",
                {"archived": False, "exclude_other_user_collections": True},
            ),
            (
                "/api/collection",
                {"archived": True, "exclude_other_user_collections": True},
            ),
            ("/api/card", {"f": "all"}),
            ("/api/card", {"f": "archived"}),
            ("/api/dashboard", {"f": "all"}),
            ("/api/dashboard", {"f": "archived"}),
            ("/api/native-query-snippet", {}),
            ("/api/native-query-snippet", {"archived": True}),
            ("/api/pulse", {}),
            ("/api/pulse", {"archived": True}),
            ("/api/database", {}),
            ("/api/user", {"status": "all"}),
        ],
    )
    collections, cards, dashboards, snippets, pulses = (
        rows(raw[i]) + rows(raw[i + 1]) for i in range(0, 10, 2)
    )
    databases = {row["id"]: row["name"] for row in rows(raw[10])}
    minor, tag = client.supported_version
    index = Index(
        version=tag,
        minor=minor,
        metadata=Metadata(client, databases),
        databases=databases,
        users={row["id"]: row["email"] for row in rows(raw[11]) if row.get("email")},
    )

    summaries_of = [
        RemoteItem.model_validate(row, extra="ignore") for row in collections
    ]
    excluded = {c.id for c in summaries_of if not c.regular_collection}
    root_ids = (
        {c.id for c in summaries_of if c.entity_id in roots}
        if roots is not None
        else None
    )
    scoped: set[int] = set()
    for collection, row in zip(summaries_of, collections, strict=True):
        ancestry = location_ids(collection.location)
        if collection.id in excluded or any(a in excluded for a in ancestry):
            excluded.add(collection.id)
        elif (
            root_ids is None
            or collection.id in root_ids
            or any(a in root_ids for a in ancestry)
        ):
            scoped.add(collection.id)
            index.add(COLLECTION, row)
        if collection.entity_id and collection.id not in scoped:
            index.add_outside(COLLECTION, collection.id, collection.entity_id)

    def in_scope(row: dict[str, Any]) -> bool:
        collection = row.get("collection_id")
        return root_ids is None if collection is None else collection in scoped

    for row in cards:
        if in_scope(row):
            index.add(CARD, row)
        elif row.get("entity_id"):
            index.add_outside(CARD, row["id"], row["entity_id"])

    listed = sorted((row for row in dashboards if in_scope(row)), key=lambda r: r["id"])
    for row in dashboards:
        if not in_scope(row) and row.get("entity_id"):
            index.add_outside(DASHBOARD, row["id"], row["entity_id"])
    details = client.map(lambda row: client.get(f"/api/dashboard/{row['id']}"), listed)
    for summary, detail in zip(listed, details, strict=True):
        index.add(DASHBOARD, with_detail(summary, detail))

    for row in snippets:
        index.add(SNIPPET, row)
    for row in pulses:
        if row.get("dashboard_id") in index.items.get(DASHBOARD, {}):
            index.add(SUBSCRIPTION, row)
    return index


def add_hidden_subscriptions(
    client: MetabaseClient, index: Index, entity_ids: set[str]
) -> None:
    missing = sorted(e for e in entity_ids if index.find(e) is None)
    if not missing:
        return
    found = client.post(
        "/api/eid-translation/translate",
        {"entity_ids": {"pulse": missing}},
        read_only=True,
    )
    ids = [t["id"] for t in found["entity_ids"].values() if t.get("status") == "ok"]
    for row in client.map(lambda i: client.get(f"/api/pulse/{i}"), ids):
        if row.get("dashboard_id") in index.items.get(DASHBOARD, {}):
            index.add(SUBSCRIPTION, row)
