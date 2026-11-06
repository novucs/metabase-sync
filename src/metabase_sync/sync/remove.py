from dataclasses import dataclass, field

from metabase_sync.format import representations
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    KINDS,
    SNIPPET,
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
    Kept,
    Operation,
    Ref,
    classify,
    digest,
)
from metabase_sync.sync.validate import items_used_by

DELETABLE = (CARD, DASHBOARD, COLLECTION)
TIMELINE = "timeline"
DOCUMENT = "document"
KEEP = "keep"
ONLY_IN_METABASE = "only in Metabase"
ROOT = ("root", 0)
SNIPPETS = ("snippets", 0)
OUTSIDE = ("outside", 0)

type Key = tuple[str, int]


@dataclass(frozen=True)
class Need:
    source: Key | None
    kind: str
    name: str
    holds: bool = False
    path: str | None = None

    @property
    def keeps(self) -> str:
        if self.path:
            return f"used by {self.path}"
        verb = "holds" if self.holds else "used by"
        return f"{verb} {self.kind} {self.name!r}"

    @property
    def blocks(self) -> str:
        if self.path:
            return f"is used by {self.path}"
        verb = "holds" if self.holds else "is used by"
        return f"{verb} {self.kind} {self.name!r}"


@dataclass
class Removed:
    operations: list[Operation] = field(default_factory=list)
    drift: list[Drift] = field(default_factory=list)
    conflicts: list[Drift] = field(default_factory=list)
    blocked: list[Drift] = field(default_factory=list)
    cascaded: list[Ref] = field(default_factory=list)
    unmanaged: list[Ref] = field(default_factory=list)
    kept: list[Kept] = field(default_factory=list)


class Graph:
    def __init__(self, tree: LocalTree, remote: Remote, writing: set[str]) -> None:
        index = remote.index
        self.remote = remote
        self.writing = writing
        self.nodes: dict[Key, tuple[str, RemoteItem]] = {
            (kind, item.id): (kind, item) for kind in KINDS for item in index.live(kind)
        }
        self.nodes |= {(DOCUMENT, d.id): (DOCUMENT, d) for d in index.documents}
        self.nodes |= {
            (TIMELINE, t.id): (TIMELINE, t) for t in index.timelines if not t.archived
        }
        self.files: dict[Key, Need] = {}
        for entity in tree.entities.values():
            source = self.key(entity.entity_id)
            need = Need(source, entity.kind, entity.name, path=str(entity.path))
            for ref in items_used_by(entity.kind, entity.data):
                if (key := self.key(ref)) is not None:
                    self.files.setdefault(key, need)
        self._needs: dict[Key, list[tuple[Key, Need]]] = {}
        self._held: dict[Key, list[tuple[str, RemoteItem]]] = {}

    def key(self, entity_id: str) -> Key | None:
        hit = self.remote.index.find(entity_id)
        key = (hit[0], hit[1].id) if hit else None
        return key if key in self.nodes else None

    def container(self, key: Key) -> Key:
        kind, item = self.nodes[key]
        if kind == SNIPPET:
            return SNIPPETS
        if kind == SUBSCRIPTION or (kind == CARD and item.dashboard_id):
            return (DASHBOARD, item.dashboard_id or 0)
        collection = item.collection(kind)
        if collection is None:
            return ROOT
        return (
            (COLLECTION, collection)
            if (COLLECTION, collection) in self.nodes
            else OUTSIDE
        )

    def held(self, key: Key) -> list[tuple[str, RemoteItem]]:
        if key not in self._held:
            kind, item = self.nodes[key]
            self._held[key] = held(self.remote.index, kind, item)
        return self._held[key]

    def needs_of(self, key: Key) -> list[tuple[Key, Need]]:
        if key not in self._needs:
            kind, item = self.nodes[key]
            found: list[tuple[Key, Need]] = []
            if kind == SUBSCRIPTION or item.entity_id in self.writing:
                self._needs[key] = found
                return found
            if (container := self.container(key)) in self.nodes:
                found.append((container, Need(key, kind, item.name, holds=True)))
            if kind in KINDS:
                data = self.remote.item(kind, item)
                for ref in items_used_by(kind, data, containers=False):
                    if (target := self.key(ref)) is not None and target != key:
                        found.append((target, Need(key, kind, item.name)))
            self._needs[key] = found
        return self._needs[key]


def removals(
    tree: LocalTree,
    remote: Remote,
    removal: str,
    restores: list[Operation],
    force: bool,
    unmanaged_mode: str = KEEP,
    whole_instance: bool = True,
    writing: set[str] | None = None,
) -> Removed:
    index = remote.index
    removed = Removed()
    graph = Graph(tree, remote, writing or set())
    only_here = {
        key
        for key, (kind, item) in graph.nodes.items()
        if kind in KINDS
        and item.entity_id
        and item.entity_id not in tree.entities
        and item.entity_id not in tree.state
    }
    in_documents = {key for key in only_here if graph.nodes[key][1].document_id}
    only_here -= in_documents
    leaving = removed_from_files(tree, remote, graph, removal, removed)

    homes = (
        managed_containers(tree, graph, whole_instance)
        if unmanaged_mode != KEEP
        else set()
    )
    if unmanaged_mode != KEEP:
        candidates = inside(graph, only_here, homes)
    elif force and removal != DELETE:
        candidates = inside(graph, only_here, set(leaving))
    else:
        candidates = set()
    for key in candidates:
        leaving[key] = removal if unmanaged_mode == KEEP else unmanaged_mode

    kept: dict[Key, str] = {}
    while leaving:
        staying = [key for key in graph.nodes if key not in leaving]
        needs: dict[Key, list[Need]] = {}
        for source in staying:
            for target, need in graph.needs_of(source):
                needs.setdefault(target, []).append(need)
        vital = vital_set(graph, staying, only_here)
        dropped: list[Key] = []
        for key, mode in leaving.items():
            kind, _ = graph.nodes[key]
            found = [graph.files[key]] if key in graph.files else []
            found += needs.get(key, [])
            unique: dict[object, Need] = {}
            for need in found:
                unique.setdefault(need.source or need, need)
            who = list(unique.values())
            destroyed = (
                destroyed_by_delete(graph, tree, key, leaving, unmanaged_mode)
                if mode == DELETE and kind in DELETABLE
                else []
            )
            if key in only_here:
                container = graph.container(key)
                if who:
                    kept[key] = who[0].keeps
                elif container not in homes and container not in leaving:
                    kept[key] = _inside_kept(graph, container)
                elif destroyed:
                    kept[key] = f"deleting it would also delete {destroyed[0]}"
                else:
                    continue
            else:
                hard = [
                    need
                    for need in who
                    if need.source is None
                    or need.source not in only_here
                    or need.source in vital
                ]
                if hard:
                    removed.blocked.append(_drift(graph, key, f"but {_joined(hard)}"))
                elif destroyed:
                    removed.blocked.append(
                        _drift(
                            graph,
                            key,
                            f"but deleting it would also delete {', '.join(destroyed)}, "
                            "which this plan doesn't delete (archived items count); "
                            "use `removal: archive` to archive it instead",
                        )
                    )
                elif who and not force:
                    verb = "is" if len(who) == 1 else "are"
                    effect = (
                        "--force removes it and its contents"
                        if any(need.holds for need in who)
                        else "--force removes it anyway"
                    )
                    removed.conflicts.append(
                        _drift(
                            graph,
                            key,
                            f"but {_joined(who)}, which {verb} not in the files "
                            f"({effect})",
                        )
                    )
                else:
                    continue
            dropped.append(key)
        if not dropped:
            break
        for key in dropped:
            del leaving[key]

    swept = {
        key for key in only_here - leaving.keys() if goes_with(graph, key, leaving)
    }
    removed.cascaded += [_ref(graph, key) for key in sorted(swept)]
    if unmanaged_mode != KEEP:
        removed.kept = [
            Kept(**_ref(graph, key).model_dump(), reason=reason)
            for key, reason in sorted(kept.items())
            if key not in swept
        ]
    left = (only_here - leaving.keys() - kept.keys() - swept) | in_documents
    if unmanaged_mode == KEEP:
        left |= kept.keys() - swept
    removed.unmanaged = [_ref(graph, key) for key in sorted(left)]
    removed.operations += operations(graph, index, tree, leaving, removed)
    if unmanaged_mode == DELETE:
        trashed = {
            inner.entity_id: Ref(
                kind=kind, entity_id=inner.entity_id or "", name=inner.name
            )
            for key, mode in leaving.items()
            if mode == DELETE and key[0] in DELETABLE
            for kind, inner in graph.held(key)
            if trashed_here(tree, kind, inner)
        }
        removed.cascaded += list(trashed.values())
    removed.operations += revived(tree, index, restores)
    return removed


def removed_from_files(
    tree: LocalTree, remote: Remote, graph: Graph, removal: str, removed: Removed
) -> dict[Key, str]:
    leaving: dict[Key, str] = {}
    for eid in sorted(tree.state.keys() - tree.entities.keys()):
        key = graph.key(eid)
        if key is None:
            continue
        kind, item = graph.nodes[key]
        prior = tree.prior(eid)
        theirs = representations.comparable(remote.item(kind, item))
        if classify(prior, None, remote_hash(prior, theirs, {})) == CONFLICT:
            ref = _ref(graph, key).model_dump()
            removed.drift.append(
                Drift(**ref, change="removed here, changed in Metabase")
            )
        leaving[key] = removal
    return leaving


def managed_containers(tree: LocalTree, graph: Graph, whole_instance: bool) -> set[Key]:
    managed: set[Key] = set()
    for eid in tree.state:
        if (key := graph.key(eid)) is None:
            continue
        managed.add(key)
        container = graph.container(key)
        if container == SNIPPETS or (
            container == ROOT and whole_instance and key[0] != COLLECTION
        ):
            managed.add(container)
    return managed


def inside(graph: Graph, only_here: set[Key], containers: set[Key]) -> set[Key]:
    found: set[Key] = set()
    grown = True
    while grown:
        grown = False
        for key in only_here - found:
            if graph.container(key) in containers or graph.container(key) in found:
                found.add(key)
                grown = True
    return found


def vital_set(graph: Graph, staying: list[Key], only_here: set[Key]) -> set[Key]:
    present = set(staying)
    vital = {key for key in staying if key not in only_here}
    vital |= present & graph.files.keys()
    waiting = list(vital)
    while waiting:
        for target, _ in graph.needs_of(waiting.pop()):
            if target in present and target not in vital:
                vital.add(target)
                waiting.append(target)
    return vital


def trashed_here(tree: LocalTree, kind: str, item: RemoteItem) -> bool:
    return (
        kind in KINDS
        and bool(item.archived)
        and item.entity_id not in tree.state
        and item.entity_id not in tree.entities
    )


def destroyed_by_delete(
    graph: Graph,
    tree: LocalTree,
    key: Key,
    leaving: dict[Key, str],
    unmanaged_mode: str,
) -> list[str]:
    destroyed: list[str] = []
    for inner_kind, inner in graph.held(key):
        if leaving.get((inner_kind, inner.id)) == DELETE:
            continue
        if unmanaged_mode == DELETE and trashed_here(tree, inner_kind, inner):
            continue
        destroyed.append(f"{inner_kind} {inner.name!r}")
    return destroyed


def goes_with(graph: Graph, key: Key, leaving: dict[Key, str]) -> bool:
    kind, item = graph.nodes[key]
    if kind in (CARD, SUBSCRIPTION) and (DASHBOARD, item.dashboard_id or 0) in leaving:
        return True
    if leaving.get(key) == DELETE:
        return False
    archiving = {other for other, mode in leaving.items() if mode != DELETE}
    chain = collection_chain(graph.remote.index, item.collection(kind))
    return any((COLLECTION, c) in archiving for c in chain)


def operations(
    graph: Graph,
    index: Index,
    tree: LocalTree,
    leaving: dict[Key, str],
    removed: Removed,
) -> list[Operation]:
    found: list[Operation] = []
    for key, mode in sorted(leaving.items()):
        kind, item = graph.nodes[key]
        if goes_with(graph, key, leaving):
            removed.cascaded.append(_ref(graph, key))
            continue
        eid = item.entity_id or ""
        found.append(
            Operation(
                action=DELETE if mode == DELETE and kind in DELETABLE else ARCHIVE,
                kind=kind,
                entity_id=eid,
                name=item.name,
                path=None,
                remote_hash=digest(item.updated_at),
                reason=None if eid in tree.state else ONLY_IN_METABASE,
            )
        )
    return found


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
        inside = (
            [
                (inner_kind, inner)
                for inner_kind in (COLLECTION, CARD, DASHBOARD)
                for inner in index.all(inner_kind)
                if item.id in collection_chain(index, inner.collection(inner_kind))
            ]
            + [
                (TIMELINE, timeline)
                for timeline in index.timelines
                if item.id in collection_chain(index, timeline.collection_id)
            ]
            + [
                (DOCUMENT, document)
                for document in index.documents
                if item.id in collection_chain(index, document.collection_id)
            ]
        )
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


def collection_chain(index: Index, collection_id: int | None) -> list[int]:
    chain: list[int] = []
    cursor = index.get(COLLECTION, collection_id)
    while cursor is not None:
        chain.append(cursor.id)
        cursor = index.get(COLLECTION, cursor.parent_id)
    return chain


def _inside_kept(graph: Graph, container: Key) -> str:
    kind, item = graph.nodes[container]
    return f"inside {kind} {item.name!r}, which is kept"


def _joined(needs: list[Need]) -> str:
    return " and ".join(dict.fromkeys(need.blocks for need in needs))


def _ref(graph: Graph, key: Key) -> Ref:
    kind, item = graph.nodes[key]
    return Ref(kind=kind, entity_id=item.entity_id or "", name=item.name)


def _drift(graph: Graph, key: Key, change: str) -> Drift:
    kind, item = graph.nodes[key]
    return Drift(
        kind=kind,
        entity_id=item.entity_id or "",
        name=item.name,
        change=f"removed here, {change}",
    )
