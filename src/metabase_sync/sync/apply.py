import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from metabase_sync.errors import StateError
from metabase_sync.format import representations
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    Item,
)
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.ids import swap_ids
from metabase_sync.metabase.client import MetabaseClient, UncertainWriteError
from metabase_sync.metabase.index import RemoteItem, rows
from metabase_sync.metabase.references import without_clicks_to
from metabase_sync.metabase.remote import Remote
from metabase_sync.store import yamlio
from metabase_sync.store.journal import (
    Created,
    Done,
    Journal,
    Posting,
    Renamed,
    Started,
)
from metabase_sync.store.state import Managed, write_state
from metabase_sync.store.tree import CONFIG, CONTENT_ROOTS, LocalTree
from metabase_sync.sync.operations import (
    ARCHIVE,
    CONTENTS,
    CREATE,
    DELETE,
    IMPORT,
    RESTORE,
    UPDATE,
    Operation,
    Plan,
)
from metabase_sync.sync.order import stages


def rename_ids(root: Path, mapping: dict[str, str]) -> int:
    if not mapping:
        return 0
    paths = [p for base in CONTENT_ROOTS for p in sorted((root / base).rglob("*.yaml"))]
    if (root / CONFIG).exists():
        paths.append(root / CONFIG)
    changed = 0
    for path in paths:
        data = yamlio.read(path)
        updated = swap_ids(data, mapping)
        if updated != data:
            yamlio.write(path, updated)
            changed += 1
    return changed


def _ids_in(item: Item) -> set[str]:
    if not isinstance(item, Dashboard):
        return {item.entity_id}
    return {item.entity_id, *(n.entity_id for n in [*item.tabs, *item.dashcards])}


def record_ids(tree: LocalTree, mapping: dict[str, str]) -> int:
    changed = rename_ids(tree.root, mapping)
    touched = set(mapping) | set(mapping.values())
    for entity in tree.entities.values():
        if (
            entity.needs_ids
            and entity.path is not None
            and _ids_in(entity.data) & touched
        ):
            renamed = representations.swapped(entity.data, mapping)
            yamlio.write(tree.root / entity.path, representations.write(renamed))
            entity.needs_ids = False
            changed += 1
    return changed


def _place(resource: ModuleType, body: dict[str, Any]) -> tuple[str, int | None]:
    key = resource.CONTAINER
    return body["name"], body.get(key) if key else None


def _same_place(
    kind: str, item: RemoteItem, name: str | None, container: int | None
) -> bool:
    return item.name == name and item.container(kind) == container


def find_created(
    client: MetabaseClient,
    api: ModuleType,
    kind: str,
    name: str | None,
    container: int | None,
    existing: list[int],
) -> list[RemoteItem]:
    resource = api.RESOURCES[kind]
    listed = rows(client.get(resource.ENDPOINT, **resource.LISTING))
    return [
        item
        for item in (RemoteItem.model_validate(row, extra="ignore") for row in listed)
        if item.id not in existing and _same_place(kind, item, name, container)
    ]


class Executor:
    def __init__(
        self,
        client: MetabaseClient,
        tree: LocalTree,
        remote: Remote,
        journal: Journal,
        on_done: Callable[[Operation, str], None] = lambda op, detail: None,
    ) -> None:
        self.client = client
        self.tree = tree
        self.remote = remote
        self.index = remote.index
        self.api = remote.api
        self.journal = journal
        self.on_done = on_done
        self._lock = threading.Lock()
        self._collection_create = threading.Lock()
        self.renamed: dict[str, str] = {}
        self.created: list[tuple[str, int]] = []

    def run(self, plan: Plan) -> None:
        try:
            if plan.renames:
                with self._lock:
                    self._rename(plan.renames)
            for stage in stages(plan.operations):
                self.client.map(self._run, stage)
        finally:
            with self._lock:
                record_ids(self.tree, self.renamed)

    def _entity(self, op: Operation) -> Item:
        with self._lock:
            current = self.renamed.get(op.entity_id, op.entity_id)
            entity = self.tree.get(current) or self.tree.get(op.entity_id)
            if entity is None:
                raise StateError(f"{op.kind} {op.entity_id} is no longer in the files")
            return entity.data

    def _url(self, op: Operation) -> str:
        item_id = self.index.id(op.kind, self.renamed.get(op.entity_id, op.entity_id))
        return f"{self.api.RESOURCES[op.kind].ENDPOINT}/{item_id}"

    def _run(self, op: Operation) -> None:
        self.journal.record(
            Started(
                op=op.id,
                action=op.action,
                kind=op.kind,
                entity_id=op.entity_id,
                name=op.name,
            )
        )
        detail = {
            CREATE: self._create,
            IMPORT: lambda op: "",
            UPDATE: self._update,
            RESTORE: self._restore,
            ARCHIVE: self._archive,
            DELETE: self._delete,
        }[op.action](op)
        self.journal.record(Done(op=op.id))
        self.on_done(op, detail)

    def _create(self, op: Operation) -> str:
        if op.kind == DASHBOARD and op.part == CONTENTS:
            return self._put_contents(op)
        body = self._body(op)
        name, container = _place(self.api.RESOURCES[op.kind], body)
        with self._lock:
            existing = [
                item.id
                for item in self.index.all(op.kind)
                if _same_place(op.kind, item, name, container)
            ]
        if op.kind == COLLECTION:
            with self._collection_create:
                return self._post(op, body, existing)
        return self._post(op, body, existing)

    def _post(self, op: Operation, body: dict[str, Any], existing: list[int]) -> str:
        resource = self.api.RESOURCES[op.kind]
        name, container = _place(resource, body)
        endpoint = resource.ENDPOINT
        self.journal.record(
            Posting(
                op=op.id,
                kind=op.kind,
                entity_id=op.entity_id,
                name=name,
                container=container,
                existing=existing,
            )
        )
        try:
            response = self.client.post(endpoint, body)
        except UncertainWriteError:
            with self._lock:
                ours = [i for k, i in self.created if k == op.kind]
            matches = find_created(
                self.client, self.api, op.kind, name, container, [*existing, *ours]
            )
            if len(matches) > 1:
                raise StateError(
                    f"Creating {op.kind} {op.name!r} timed out and {len(matches)} matching items now exist",
                    hint="Remove the duplicates in Metabase, then re-run",
                ) from None
            if not matches:
                raise StateError(
                    f"Metabase didn't confirm creating {op.kind} {op.name!r}, "
                    "and it may still be working on it",
                    hint="Run `metabase-sync apply` again in a minute; it checks for "
                    "the item before creating it",
                ) from None
            response = self.client.get(f"{endpoint}/{matches[0].id}")
        created = self._record_created(op, response)
        kept = self._request(op.kind, self.remote.read(op.kind, response), CREATE)
        dropped = {
            k: v
            for k, v in body.items()
            if k in kept and kept[k] != v and k not in resource.FIXED
        }
        if dropped:
            self.client.put(f"{endpoint}/{created.id}", dropped)
        return f"id {created.id}"

    def _record_created(self, op: Operation, response: dict[str, Any]) -> RemoteItem:
        with self._lock:
            created = self.index.add(op.kind, response)
            self.created.append((op.kind, created.id))
        self.journal.record(
            Created(
                op=op.id,
                kind=op.kind,
                entity_id=op.entity_id,
                created_id=created.id,
                created_entity_id=created.entity_id,
            )
        )
        if created.entity_id:
            with self._lock:
                self._rename({op.entity_id: created.entity_id})
        return created

    def _rename(self, mapping: dict[str, str]) -> None:
        self.journal.record(Renamed(mapping=mapping))
        for old, new in mapping.items():
            self.renamed[old] = new
            if old in self.tree.entities:
                self.tree.entities[new] = self.tree.entities.pop(old)
            if old in self.tree.state:
                self.tree.state[new] = self.tree.state.pop(old)
        for entity in self.tree.entities.values():
            entity.data = representations.swapped(entity.data, mapping)

    def _update(self, op: Operation) -> str:
        if op.kind == DASHBOARD and op.part == CONTENTS:
            return self._put_contents(op)
        self.client.put(self._url(op), self._body(op))
        return ", ".join(op.changes)

    def _body(self, op: Operation) -> dict[str, Any]:
        item = self._entity(op)
        if isinstance(item, Card) and op.action == CREATE and op.deferred:
            with self._lock:
                held = {*op.deferred, *(self.renamed.get(t, t) for t in op.deferred)}
            settings = without_clicks_to(item.visualization_settings, held)
            item = item.model_copy(update={"visualization_settings": settings})
        return self._request(op.kind, item, op.action)

    def _request(self, kind: str, item: Item, action: str) -> dict[str, Any]:
        resource = self.api.RESOURCES[kind]
        if isinstance(item, Subscription):
            dashboard = self._dashboard(self.index.id(DASHBOARD, item.dashboard_id))
            request = resource.request(item, self.index, dashboard)
        else:
            request = resource.request(item, self.index)
        fixed = set() if action == CREATE else resource.FIXED
        return request.model_dump(mode="json", exclude=fixed)

    def _dashboard(self, dash_id: int) -> RemoteItem:
        return RemoteItem.model_validate(
            self.client.get(f"/api/dashboard/{dash_id}"), extra="ignore"
        )

    def _restore(self, op: Operation) -> str:
        self.client.put(self._url(op), {"archived": False})
        return ""

    def _archive(self, op: Operation) -> str:
        url = self._url(op)
        if RemoteItem.model_validate(self.client.get(url), extra="ignore").archived:
            return "already archived"
        self.client.put(url, {"archived": True})
        return ""

    def _delete(self, op: Operation) -> str:
        url = self._url(op)
        if not RemoteItem.model_validate(self.client.get(url), extra="ignore").archived:
            self.client.put(url, {"archived": True})
        self.client.delete(url)
        return ""

    def _put_contents(self, op: Operation) -> str:
        desired = self._dashboard_in_files(op.entity_id)
        dash_id = self.index.id(DASHBOARD, desired.entity_id)
        dashboard = self._dashboard(dash_id)
        self.index.learn_dashboard(dashboard)
        desired = self._claim_auto_dashcards(desired, dashboard)
        resource = self.api.RESOURCES[DASHBOARD]
        body = resource.contents(desired, self.index, dashboard).model_dump(mode="json")
        result = self.client.put(f"{resource.ENDPOINT}/{dash_id}", body)
        updated = (
            RemoteItem.model_validate(result, extra="ignore")
            if isinstance(result, dict) and "dashcards" in result
            else self._dashboard(dash_id)
        )
        self._learn_new_parts(desired, body, updated)
        return f"{len(body['dashcards'])} dashcard(s), {len(body['tabs'])} tab(s)"

    def _claim_auto_dashcards(
        self, desired: Dashboard, current: RemoteItem
    ) -> Dashboard:
        known = {dc.entity_id for dc in desired.dashcards}
        present = {dc.entity_id for dc in current.dashcards}
        unclaimed = [dc for dc in current.dashcards if dc.entity_id not in known]
        mapping: dict[str, str] = {}
        for dc in desired.dashcards:
            if dc.entity_id in present or not dc.card_id:
                continue
            card_id = self.index.id(CARD, dc.card_id)
            match = next((u for u in unclaimed if u.card_id == card_id), None)
            if match and match.entity_id:
                unclaimed.remove(match)
                mapping[dc.entity_id] = match.entity_id
        if mapping:
            with self._lock:
                self._rename(mapping)
            return self._dashboard_in_files(desired.entity_id)
        return desired

    def _dashboard_in_files(self, entity_id: str) -> Dashboard:
        with self._lock:
            found = self.tree.get(self.renamed.get(entity_id, entity_id))
        if found is None or not isinstance(found.data, Dashboard):
            raise StateError(f"dashboard {entity_id} is no longer in the files")
        return found.data

    def _learn_new_parts(
        self, desired: Dashboard, body: dict[str, Any], result: RemoteItem
    ) -> None:
        mapping: dict[str, str] = {}
        real_tab: dict[int | None, int | None] = {None: None}
        for tab_entity, sent in zip(desired.tabs, body["tabs"], strict=True):
            if sent["id"] > 0:
                real_tab[sent["id"]] = sent["id"]
                continue
            match = next(
                (
                    t
                    for t in result.tabs
                    if t.name == sent["name"] and t.position == sent["position"]
                ),
                None,
            )
            if match is None:
                continue
            real_tab[sent["id"]] = match.id
            if match.entity_id and match.entity_id != tab_entity.entity_id:
                mapping[tab_entity.entity_id] = match.entity_id
        used = {dc["id"] for dc in body["dashcards"] if dc["id"] > 0}
        remaining = [dc for dc in result.dashcards if dc.id not in used]
        for dc_entity, sent in zip(desired.dashcards, body["dashcards"], strict=True):
            if sent["id"] > 0:
                continue
            match = next(
                (
                    r
                    for r in remaining
                    if r.card_id == sent["card_id"]
                    and r.dashboard_tab_id == real_tab.get(sent["dashboard_tab_id"])
                    and r.row == sent["row"]
                    and r.col == sent["col"]
                ),
                None,
            )
            if match is not None:
                remaining.remove(match)
                if match.entity_id and match.entity_id != dc_entity.entity_id:
                    mapping[dc_entity.entity_id] = match.entity_id
        if mapping:
            with self._lock:
                self._rename(mapping)
        self.index.learn_dashboard(result)


def reconcile(
    client: MetabaseClient, api: ModuleType, journal: Journal, tree: LocalTree
) -> list[str]:
    entries = journal.entries()
    if not entries:
        return []
    notes: list[str] = []
    created = {e.op: e for e in entries if isinstance(e, Created)}
    mapping: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, Renamed):
            mapping.update(entry.mapping)
        elif isinstance(entry, Created) and entry.created_entity_id:
            mapping[entry.entity_id] = entry.created_entity_id
    for entry in entries:
        if not isinstance(entry, Posting) or entry.op in created:
            continue
        ours = [c.created_id for c in created.values() if c.kind == entry.kind]
        matches = find_created(
            client,
            api,
            entry.kind,
            entry.name,
            entry.container,
            [*entry.existing, *ours],
        )
        if len(matches) > 1:
            raise StateError(
                f"An interrupted apply may have created {entry.kind} {entry.name!r} {len(matches)} times",
                hint="Remove the duplicates in Metabase, then re-run",
            )
        if matches:
            if matches[0].entity_id:
                mapping[entry.entity_id] = matches[0].entity_id
            notes.append(
                f"found {entry.kind} {entry.name!r} created by an interrupted apply"
            )
    changed = record_ids(tree, mapping)
    tree.state = {mapping.get(eid, eid): entry for eid, entry in tree.state.items()}
    for entry in entries:
        if isinstance(entry, Started):
            eid = mapping.get(entry.entity_id, entry.entity_id)
            tree.state[eid] = Managed(kind=entry.kind)
    write_state(tree.root, tree.state)
    if changed:
        notes.append(
            f"recorded entity ids assigned during an interrupted apply in {changed} file(s)"
        )
    journal.clear()
    return notes
