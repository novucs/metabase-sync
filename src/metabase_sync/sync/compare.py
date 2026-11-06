import difflib
from dataclasses import dataclass, field
from typing import Any

from metabase_sync.format import representations
from metabase_sync.format.representations import (
    COMPARED_WHEN_PRESENT,
    DASHBOARD,
    KINDS,
    SUBSCRIPTION,
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
    full = representations.comparable(remote.item(*hit)) if hit else {}
    theirs = remote_view(full, local) if hit else None
    live = item is not None and not item.archived
    prior = tree.prior(eid)
    state = classify(
        prior, digest(local), remote_hash(prior, full, local) if live else None
    )

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

    def drift(change: str) -> Drift:
        return Drift(
            kind=entity.kind,
            entity_id=eid,
            name=tree.label(entity),
            path=path,
            change=change,
        )

    if hit is None and remote.index.is_outside(eid):
        compared.conflicts.append(
            drift(
                (
                    "lost its channels, or its dashboard moved out of the managed "
                    "collections, in Metabase"
                    if entity.kind == SUBSCRIPTION
                    else "moved out of the managed collections in Metabase"
                )
                + " (--force skips it)"
            )
        )
        return
    if hit is not None and eid not in tree.state:
        compared.operations.append(operation(IMPORT))
    if state == SAME:
        return
    labelled = state in (THEIRS, CONFLICT)
    if labelled:
        compared.drift.append(drift(_change(item, state == CONFLICT)))

    parts = [BASE, CONTENTS] if entity.kind == DASHBOARD else [BASE]
    if theirs is None:
        if ambiguous:
            compared.conflicts.append(
                drift(
                    "matches several items in Metabase by name. Set its entity_id "
                    "to pick one (--force creates a new one)"
                )
            )
            if not force:
                return
        compared.operations += [operation(CREATE, part=part) for part in parts]
        return
    if theirs != local:
        compared.diffs[eid] = render_diff(theirs, local, "metabase", path or "files")
    if item is not None and item.archived:
        compared.operations.append(
            operation(
                RESTORE,
                remote_hash=digest(theirs),
                reason=None if labelled else "archived in Metabase",
            )
        )
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
                    path=str(entity.path) if entity.path else None,
                    change=", ".join(missing),
                )
            )
    return dropped


def _change(item: RemoteItem | None, here: bool) -> str:
    there = "deleted" if item is None else "archived" if item.archived else "changed"
    if not here:
        return f"{there} in Metabase"
    if there == "changed":
        return "changed here and in Metabase"
    return f"changed here, {there} in Metabase"


def remote_view(remote: dict[str, Any], local: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in remote.items() if k not in COMPARED_WHEN_PRESENT or k in local
    }


def remote_hash(
    prior: str | None, remote: dict[str, Any], local: dict[str, Any]
) -> str:
    seen = digest(remote_view(remote, local))
    unchanged = prior in (digest(remote), digest(remote_view(remote, {})))
    return prior if prior is not None and unchanged and seen != digest(local) else seen


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
