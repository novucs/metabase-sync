from dataclasses import dataclass, field

from metabase_sync.format import representations
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    KINDS,
)
from metabase_sync.metabase.index import Index, RemoteItem
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import LocalTree
from metabase_sync.sync.compare import remote_view
from metabase_sync.sync.operations import (
    ARCHIVE,
    CONFLICT,
    DELETE,
    Drift,
    Operation,
    Ref,
    classify,
    digest,
)

DELETABLE = (CARD, DASHBOARD, COLLECTION)


@dataclass
class Removed:
    operations: list[Operation] = field(default_factory=list)
    conflicts: list[Drift] = field(default_factory=list)
    cascaded: list[Ref] = field(default_factory=list)


def removals(
    tree: LocalTree,
    remote: Remote,
    removal: str,
    restores: list[Operation],
    force: bool,
) -> Removed:
    index = remote.index
    removed = Removed()
    removing: dict[str, tuple[str, RemoteItem]] = {}
    for eid in sorted(tree.managed - set(tree.entities)):
        hit = index.find(eid)
        if hit is None or hit[1].archived:
            continue
        kind, item = hit
        if classify(tree.prior(eid), None, remote_hash(tree, remote, eid)) == CONFLICT:
            removed.conflicts.append(_drift(kind, eid, item, "changed in Metabase"))
            if not force:
                continue
        swept = swept_along(tree, index, kind, item)
        if swept:
            held = (
                "1 item that is" if len(swept) == 1 else f"{len(swept)} items that are"
            )
            removed.conflicts.append(
                _drift(kind, eid, item, f"but holds {held} not in the files")
            )
            if not force:
                continue
            removed.cascaded += swept
        removing[eid] = hit

    collections = {item.id for kind, item in removing.values() if kind == COLLECTION}
    dashboards = {item.id for kind, item in removing.values() if kind == DASHBOARD}
    for eid, (kind, item) in removing.items():
        inside = set(collection_chain(index, item.collection(kind)))
        if inside & collections or (kind == CARD and item.dashboard_id in dashboards):
            removed.cascaded.append(Ref(kind=kind, entity_id=eid, name=item.name))
            continue
        action = DELETE if removal == DELETE and kind in DELETABLE else ARCHIVE
        removed.operations.append(
            Operation(
                action=action,
                kind=kind,
                entity_id=eid,
                name=item.name,
                path=None,
                remote_hash=digest(item.updated_at),
            )
        )
    removed.operations += revived(tree, index, restores)
    return removed


def revived(
    tree: LocalTree, index: Index, restores: list[Operation]
) -> list[Operation]:
    restored = {
        (hit[0], hit[1].id)
        for op in restores
        if (hit := index.find(op.entity_id)) is not None
    }
    found: list[Operation] = []
    for kind in KINDS:
        for item in index.all(kind):
            eid = item.entity_id
            if (
                not eid
                or eid in tree.entities
                or not item.archived
                or item.archived_directly
            ):
                continue
            chain = collection_chain(index, item.collection(kind))
            if any((COLLECTION, cid) in restored for cid in chain) or (
                kind == CARD and (DASHBOARD, item.dashboard_id) in restored
            ):
                found.append(
                    Operation(
                        action=ARCHIVE,
                        kind=kind,
                        entity_id=eid,
                        name=item.name,
                        path=None,
                        reason="revived by a restore",
                    )
                )
    return found


def swept_along(
    tree: LocalTree, index: Index, kind: str, item: RemoteItem
) -> list[Ref]:
    if kind not in (COLLECTION, DASHBOARD):
        return []
    swept = []
    for inner_kind in (COLLECTION, CARD, DASHBOARD):
        for inner in index.live(inner_kind):
            eid = inner.entity_id
            if not eid or eid in tree.managed or eid in tree.entities:
                continue
            inside = (
                item.id in collection_chain(index, inner.collection(inner_kind))
                if kind == COLLECTION
                else inner_kind == CARD and inner.dashboard_id == item.id
            )
            if inside:
                swept.append(Ref(kind=inner_kind, entity_id=eid, name=inner.name))
    return swept


def remote_hash(tree: LocalTree, remote: Remote, eid: str) -> str | None:
    hit = remote.index.find(eid)
    if hit is None:
        return None
    theirs = representations.comparable(remote.item(*hit))
    bare = digest(remote_view(theirs, {}))
    return bare if bare == tree.prior(eid) else digest(theirs)


def collection_chain(index: Index, collection_id: int | None) -> list[int]:
    chain: list[int] = []
    cursor = index.get(COLLECTION, collection_id)
    while cursor is not None:
        chain.append(cursor.id)
        cursor = index.get(COLLECTION, cursor.parent_id)
    return chain


def _drift(kind: str, eid: str, item: RemoteItem, change: str) -> Drift:
    return Drift(
        kind=kind, entity_id=eid, name=item.name, change=f"removed here, {change}"
    )
