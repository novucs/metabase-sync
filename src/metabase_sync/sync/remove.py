from dataclasses import dataclass, field

from metabase_sync.format import representations
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    KINDS,
    SUBSCRIPTION,
)
from metabase_sync.metabase.index import Index, RemoteItem
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import LocalTree
from metabase_sync.sync.compare import remote_hash
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
TIMELINE = "timeline"


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
    for eid in sorted(tree.state.keys() - tree.entities.keys()):
        hit = index.find(eid)
        if hit is None or hit[1].archived:
            continue
        kind, item = hit
        prior = tree.prior(eid)
        theirs = representations.comparable(remote.item(kind, item))
        if classify(prior, None, remote_hash(prior, theirs, {})) == CONFLICT:
            removed.conflicts.append(_drift(kind, eid, item, "changed in Metabase"))
            if not force:
                continue
        swept = [] if removal == DELETE else swept_along(tree, index, kind, item)
        if swept:
            names = ", ".join(f"{ref.kind} {ref.name!r}" for ref in swept)
            verb = "is" if len(swept) == 1 else "are"
            removed.conflicts.append(
                _drift(
                    kind, eid, item, f"but holds {names}, which {verb} not in the files"
                )
            )
            if not force:
                continue
            removed.cascaded += swept
        removing[eid] = hit
    if removal == DELETE:
        removed.conflicts += kept_by_deletes(index, removing)

    collections = {item.id for kind, item in removing.values() if kind == COLLECTION}
    dashboards = {item.id for kind, item in removing.values() if kind == DASHBOARD}
    for eid, (kind, item) in removing.items():
        inside = set(collection_chain(index, item.collection(kind)))
        if removal != DELETE and (
            inside & collections or (kind == CARD and item.dashboard_id in dashboards)
        ):
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


def held(index: Index, kind: str, item: RemoteItem) -> list[tuple[str, RemoteItem]]:
    if kind == DASHBOARD:
        inside = [(CARD, c) for c in index.all(CARD) if c.dashboard_id == item.id]
        dashboards = {item.id}
    elif kind == COLLECTION:
        inside = [
            (inner_kind, inner)
            for inner_kind in (COLLECTION, CARD, DASHBOARD)
            for inner in index.all(inner_kind)
            if item.id in collection_chain(index, inner.collection(inner_kind))
        ] + [
            (TIMELINE, timeline)
            for timeline in index.timelines
            if item.id in collection_chain(index, timeline.collection_id)
        ]
        dashboards = {
            inner.id for inner_kind, inner in inside if inner_kind == DASHBOARD
        }
    else:
        return []
    return inside + [
        (SUBSCRIPTION, pulse)
        for pulse in index.all(SUBSCRIPTION)
        if pulse.dashboard_id in dashboards and not pulse.archived
    ]


def swept_along(
    tree: LocalTree, index: Index, kind: str, item: RemoteItem
) -> list[Ref]:
    return [
        Ref(kind=inner_kind, entity_id=inner.entity_id, name=inner.name)
        for inner_kind, inner in held(index, kind, item)
        if inner_kind in DELETABLE
        and inner.entity_id
        and not inner.archived
        and inner.entity_id not in tree.state
        and inner.entity_id not in tree.entities
    ]


def kept_by_deletes(
    index: Index, removing: dict[str, tuple[str, RemoteItem]]
) -> list[Drift]:
    conflicts: list[Drift] = []
    stable = False
    while not stable:
        stable = True
        for eid, (kind, item) in list(removing.items()):
            kept = [
                f"{inner_kind} {inner.name!r}"
                for inner_kind, inner in held(index, kind, item)
                if inner.entity_id not in removing
            ]
            if kept:
                del removing[eid]
                stable = False
                conflicts.append(
                    _drift(
                        kind,
                        eid,
                        item,
                        f"but deleting it would also delete {', '.join(kept)}, which "
                        "this plan doesn't delete (archived items count); use "
                        "`removal: archive` to archive it instead",
                    )
                )
    return conflicts


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
