import difflib
from dataclasses import dataclass, field
from typing import Any

from metabase_sync.format import representations
from metabase_sync.format.representations import (
    COMPARED_WHEN_PRESENT,
    DASHBOARD,
    KINDS,
)
from metabase_sync.format.representations.card import Card
from metabase_sync.metabase.index import RemoteItem
from metabase_sync.metabase.references import click_targets
from metabase_sync.metabase.remote import Remote
from metabase_sync.store import yamlio
from metabase_sync.store.tree import Entity, LocalTree
from metabase_sync.sync.operations import (
    BASE,
    CONFLICT,
    CONTENTS,
    CREATE,
    IMPORT,
    LINKS,
    RESTORE,
    SAME,
    THEIRS,
    UPDATE,
    Drift,
    Operation,
    classify,
    digest,
    part_view,
)

KIND_ORDER = tuple(KINDS)


@dataclass
class Compared:
    operations: list[Operation] = field(default_factory=list)
    drift: list[Drift] = field(default_factory=list)
    conflicts: list[Drift] = field(default_factory=list)
    diffs: dict[str, str] = field(default_factory=dict)


def compare(
    tree: LocalTree, remote: Remote, ambiguous: set[str], force: bool
) -> Compared:
    compared = Compared()
    for entity in sorted(
        tree.entities.values(), key=lambda e: (KIND_ORDER.index(e.kind), str(e.path))
    ):
        _compare_one(
            entity, tree, remote, entity.entity_id in ambiguous, force, compared
        )
    compared.operations += link_updates(tree, compared.operations)
    return compared


def _compare_one(
    entity: Entity,
    tree: LocalTree,
    remote: Remote,
    ambiguous: bool,
    force: bool,
    compared: Compared,
) -> None:
    eid = entity.entity_id
    path = str(entity.path) if entity.path else None
    local = representations.comparable(entity.data)
    hit = remote.index.find(eid)
    item = hit[1] if hit else None
    theirs = (
        remote_view(representations.comparable(remote.item(*hit)), local)
        if hit
        else None
    )
    live = item is not None and not item.archived
    state = classify(tree.prior(eid), digest(local), digest(theirs) if live else None)

    def operation(action: str, **fields: Any) -> Operation:
        return Operation(
            action=action,
            kind=entity.kind,
            entity_id=eid,
            name=tree.label(entity),
            path=path,
            local_hash=digest(local),
            **fields,
        )

    if hit is not None and eid not in tree.managed:
        compared.operations.append(operation(IMPORT))
    if state == SAME:
        return
    if state in (THEIRS, CONFLICT):
        if theirs is not None:
            compared.diffs[eid] = (
                render_diff(theirs, local, "metabase", path or "files")
                if state == CONFLICT
                else render_diff(local, theirs, path or "files", "metabase")
            )
        drift = Drift(
            kind=entity.kind,
            entity_id=eid,
            name=tree.label(entity),
            path=path or "",
            change=_change(item, state),
        )
        (compared.conflicts if state == CONFLICT else compared.drift).append(drift)
        if not force:
            return

    parts = [BASE, CONTENTS] if entity.kind == DASHBOARD else [BASE]
    if theirs is None:
        if ambiguous:
            compared.conflicts.append(
                Drift(
                    kind=entity.kind,
                    entity_id=eid,
                    name=tree.label(entity),
                    path=path or "",
                    change="can't be matched to one item in Metabase by name; set "
                    "its entity_id to import one, or pass --force to create it",
                )
            )
            if not force:
                return
        compared.operations += [operation(CREATE, part=part) for part in parts]
        return
    if theirs != local:
        compared.diffs.setdefault(
            eid, render_diff(theirs, local, "metabase", path or "files")
        )
    if item is not None and item.archived:
        compared.operations.append(operation(RESTORE, remote_hash=digest(theirs)))
    for part in parts:
        before = part_view(entity.kind, part, theirs)
        changes = changed_fields(before, part_view(entity.kind, part, local))
        if changes:
            compared.operations.append(
                operation(
                    UPDATE, part=part, changes=changes, remote_hash=digest(before)
                )
            )


def link_updates(tree: LocalTree, operations: list[Operation]) -> list[Operation]:
    creating = {op.entity_id for op in operations if op.action == CREATE}
    links: list[Operation] = []
    for op in operations:
        entity = tree.get(op.entity_id)
        if op.action != CREATE or op.part != BASE or entity is None:
            continue
        if not isinstance(entity.data, Card):
            continue
        deferred = sorted(click_targets(entity.data.visualization_settings) & creating)
        if deferred:
            op.deferred = deferred
            links.append(
                op.model_copy(
                    update={
                        "action": UPDATE,
                        "part": LINKS,
                        "changes": ["visualization_settings"],
                        "reason": "links to items this apply creates",
                    }
                )
            )
    return links


def not_kept(tree: LocalTree, remote: Remote, entity_ids: set[str]) -> list[Drift]:
    dropped: list[Drift] = []
    for eid in sorted(entity_ids):
        entity, hit = tree.get(eid), remote.index.find(eid)
        if entity is None:
            continue
        local = representations.comparable(entity.data)
        theirs = (
            remote_view(representations.comparable(remote.item(*hit)), local)
            if hit
            else {}
        )
        missing = changed_fields(theirs, local)
        if missing:
            dropped.append(
                Drift(
                    kind=entity.kind,
                    entity_id=eid,
                    name=tree.label(entity),
                    path=str(entity.path) if entity.path else "",
                    change=", ".join(missing),
                )
            )
    return dropped


def _change(item: RemoteItem | None, state: str) -> str:
    if item is None:
        change = "deleted in Metabase"
    elif item.archived:
        change = "archived in Metabase"
    else:
        change = "changed in Metabase"
    if state != CONFLICT:
        return change
    if change == "changed in Metabase":
        return "changed here and in Metabase"
    return f"changed here, {change}"


def remote_view(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in remote.items() if k not in COMPARED_WHEN_PRESENT or k in local
    }


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    keys = list(dict.fromkeys([*after, *before]))
    return [k for k in keys if before.get(k) != after.get(k)]


def render_diff(
    before: dict[str, Any], after: dict[str, Any], old: str, new: str
) -> str:
    return "".join(
        difflib.unified_diff(
            yamlio.dumps(before).splitlines(keepends=True),
            yamlio.dumps(after).splitlines(keepends=True),
            fromfile=old,
            tofile=new,
        )
    )
