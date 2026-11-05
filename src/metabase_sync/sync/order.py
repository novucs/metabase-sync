from collections.abc import Iterator
from graphlib import CycleError, TopologicalSorter
from itertools import pairwise
from typing import Any

from metabase_sync.errors import ValidationFailed
from metabase_sync.format import representations
from metabase_sync.format.representations import CONTAINERS, SUBSCRIPTION
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.ids import is_entity_id
from metabase_sync.metabase.references import click_targets, without_clicks_to
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import LocalTree
from metabase_sync.sync.operations import (
    BASE,
    CONTENTS,
    CREATE,
    LINKS,
    POSITIVE,
    REMOVALS,
    RESTORE,
    UPDATE,
    Key,
    Operation,
    part_view,
)

type Edge = tuple[Key, Key]


def ordered(
    operations: list[Operation], tree: LocalTree, remote: Remote
) -> list[Operation]:
    ops = {op.key: op for op in operations}
    waits: dict[Key, set[str]] = {}
    for waiter, needed in [
        *own_steps(ops),
        *referenced_items(ops, tree),
        *dashboard_contents(ops, tree),
        *pinned_order(ops, tree),
        *same_place_creates(ops, tree),
        *removals_last(ops, remote),
    ]:
        if needed in ops and needed != waiter:
            waits.setdefault(waiter, set()).add(ops[needed].id)
    with_deps = [
        op.model_copy(update={"depends_on": sorted(waits.get(op.key, set()))})
        for op in operations
    ]
    return [op for stage in stages(with_deps) for op in stage]


def own_steps(ops: dict[Key, Operation]) -> Iterator[Edge]:
    for op in ops.values():
        if op.part == LINKS:
            yield op.key, Key(op.entity_id, BASE, CREATE)
        elif op.action == UPDATE:
            yield op.key, Key(op.entity_id, BASE, RESTORE)


def referenced_items(ops: dict[Key, Operation], tree: LocalTree) -> Iterator[Edge]:
    for op in ops.values():
        entity = tree.get(op.entity_id)
        if op.action not in POSITIVE or entity is None:
            continue
        data = representations.write(entity.data)
        if op.part == LINKS:
            refs = set(op.deferred)
        elif op.action == RESTORE:
            refs = {
                data[k] for k in CONTAINERS[op.kind] if isinstance(data.get(k), str)
            }
        else:
            sent = without_clicks_to(data, set(op.deferred))
            refs = references(part_view(op.kind, op.part, sent))
        for ref in refs:
            yield from (
                (op.key, Key(ref, BASE, action)) for action in (CREATE, RESTORE)
            )
            if op.kind == SUBSCRIPTION:
                for action in (CREATE, UPDATE):
                    yield op.key, Key(ref, CONTENTS, action)


def dashboard_contents(ops: dict[Key, Operation], tree: LocalTree) -> Iterator[Edge]:
    for op in ops.values():
        entity = tree.get(op.entity_id)
        if (
            op.part != CONTENTS
            or entity is None
            or not isinstance(entity.data, Dashboard)
        ):
            continue
        for action in POSITIVE:
            yield op.key, Key(op.entity_id, BASE, action)
        for card in tree.entities.values():
            if isinstance(card.data, Card) and card.data.dashboard_id == op.entity_id:
                for action in POSITIVE:
                    yield op.key, Key(card.entity_id, BASE, action)


def pinned_order(ops: dict[Key, Operation], tree: LocalTree) -> Iterator[Edge]:
    pinned: dict[str | None, list[tuple[int, Key]]] = {}
    for op in ops.values():
        entity = tree.get(op.entity_id)
        data = entity.data if entity else None
        if (
            op.action == CREATE
            and op.part == BASE
            and isinstance(data, Card | Dashboard)
            and data.collection_position is not None
            and not (isinstance(data, Card) and data.dashboard_id)
        ):
            pinned.setdefault(data.collection_id, []).append(
                (data.collection_position, op.key)
            )
    for group in pinned.values():
        group.sort(key=lambda pin: pin[0])
        for (_, earlier), (_, later) in pairwise(group):
            yield later, earlier


def same_place_creates(ops: dict[Key, Operation], tree: LocalTree) -> Iterator[Edge]:
    places: dict[tuple[Any, ...], list[tuple[int, Key]]] = {}
    for op in ops.values():
        entity = tree.get(op.entity_id)
        if op.action == CREATE and op.part == BASE and entity is not None:
            data = entity.data
            container = tuple(getattr(data, f) for f in CONTAINERS[op.kind])
            pin = data.collection_position if isinstance(data, Card | Dashboard) else 0
            places.setdefault((op.kind, op.name, container), []).append(
                (pin or 0, op.key)
            )
    for group in places.values():
        group.sort(key=lambda place: place[0])
        for (_, earlier), (_, later) in pairwise(group):
            yield later, earlier


def removals_last(ops: dict[Key, Operation], remote: Remote) -> Iterator[Edge]:
    removing = [op for op in ops.values() if op.action in REMOVALS]
    refs: dict[str, set[str]] = {}
    for op in removing:
        hit = remote.index.find(op.entity_id)
        data = representations.comparable(remote.item(*hit)) if hit else {}
        refs[op.entity_id] = references(without_clicks_to(data, click_targets(data)))
    for op in removing:
        for other in ops.values():
            if (
                other.action in POSITIVE
                or other.action in REMOVALS
                and op.entity_id in refs[other.entity_id]
            ):
                yield op.key, other.key


def references(entity: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif is_entity_id(value):
            found.add(value)

    walk({k: v for k, v in entity.items() if k != "entity_id"})
    return found


def stages(operations: list[Operation]) -> list[list[Operation]]:
    by_id = {op.id: op for op in operations}
    position = {op.id: i for i, op in enumerate(operations)}
    sorter = TopologicalSorter(
        {op.id: [d for d in op.depends_on if d in by_id] for op in operations}
    )
    try:
        sorter.prepare()
    except CycleError as e:
        loop = [by_id[i] for i in e.args[1]]
        raise ValidationFailed(
            [
                "these items need each other to exist first: "
                + " -> ".join(o.path or f"{o.kind} {o.name}" for o in loop)
            ]
        ) from None
    out: list[list[Operation]] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready(), key=position.__getitem__)
        out.append([by_id[i] for i in ready])
        sorter.done(*ready)
    return out
