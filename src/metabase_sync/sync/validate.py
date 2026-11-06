import logging
from dataclasses import dataclass, field
from typing import Any

from metabase_sync.errors import UnresolvedReferenceError, count
from metabase_sync.format import representations
from metabase_sync.format.representations import (
    CONTAINERS,
    DASHBOARD,
    SNIPPET,
    SUBSCRIPTION,
    collection_of,
)
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.metabase.references import DATABASE, FIELD, TABLE, walk
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import (
    CONFIG,
    LocalTree,
    ancestors,
    container_refs,
    layout,
)

log = logging.getLogger(__name__)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate(tree: LocalTree, scope: set[str] | None = None) -> Report:
    report = Report(errors=list(tree.problems))
    nested_ids: dict[str, str] = {}
    names: dict[tuple[str, str | None, str], list[str]] = {}

    for entity in tree.entities.values():
        where = str(entity.path)
        for key, kind in CONTAINERS[entity.kind].items():
            ref = getattr(entity.data, key)
            if not isinstance(ref, str):
                continue
            target = tree.get(ref)
            if target is None:
                if ref in tree.state:
                    report.errors.append(
                        f"{where}: {key} {ref} was removed from the files; restore "
                        f"its file, or point {key} at another {kind}"
                    )
                elif not (scope and entity.entity_id in scope):
                    report.warnings.append(
                        f"{where}: {key} {ref} is not in the files (it must already exist in Metabase)"
                    )
            elif target.kind != kind:
                report.errors.append(
                    f"{where}: {key} {ref} is a {target.kind}, expected a {kind}"
                )
        if isinstance(entity.data, Dashboard):
            for nested in [*entity.data.tabs, *entity.data.dashcards]:
                _claim(nested_ids, nested.entity_id, where, report)
            for dc in entity.data.dashcards:
                if entity.data.tabs and dc.dashboard_tab_id is None:
                    report.warnings.append(
                        f"{where}: the dashcard at row {dc.row}, column {dc.col} has "
                        "no dashboard_tab_id, so it is on none of the dashboard's tabs"
                    )
                card = dc.card_id
                if card is not None and tree.get(card) is None and card in tree.state:
                    report.errors.append(
                        f"{where}: the dashcard at row {dc.row}, column {dc.col} "
                        "shows a question whose file you removed; remove the dashcard too, "
                        "or restore the question's file"
                    )
        container = collection_of(entity.data)
        key = (entity.kind, container, entity.name.strip().lower())
        names.setdefault(key, []).append(where)

    for (kind, _, name), paths in names.items():
        if len(paths) > 1 and kind != SUBSCRIPTION:
            report.warnings.append(
                f"{len(paths)} {kind}s named {name!r} in the same place: {', '.join(paths)}"
            )

    for eid, where in nested_ids.items():
        if eid in tree.entities:
            report.errors.append(
                f"{where}: entity_id {eid} is also used by {tree.entities[eid].path}"
            )

    if not report.errors:
        expected = layout(list(tree.entities.values()))
        for eid, entity in tree.entities.items():
            if entity.path != expected[eid]:
                report.warnings.append(
                    f"{entity.path}: expected at {expected[eid]} (`metabase-sync fmt` moves it)"
                )
    return report


def remote_problems(
    tree: LocalTree, remote: Remote, scope: set[str] | None
) -> list[str]:
    return [
        *mismatched_kinds(tree, remote),
        *missing_references(tree, remote),
        *outside_scope(tree, remote, scope),
        *removed_dashcard_filters(tree, remote),
    ]


def mismatched_kinds(tree: LocalTree, remote: Remote) -> list[str]:
    return [
        f"{entity.path}: entity_id {entity.entity_id} belongs to a {hit[0]} in "
        f"Metabase, but this file is a {entity.kind}"
        for entity in tree.entities.values()
        if (hit := remote.index.find(entity.entity_id)) and hit[0] != entity.kind
    ]


def _references(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []

    def collect(kind: str, ref: Any) -> Any:
        found.append((kind, ref))
        return ref

    walk(value, collect)
    return found


def missing_references(tree: LocalTree, remote: Remote) -> list[str]:
    index = remote.index
    problems: list[str] = []
    for entity in tree.entities.values():
        containers = CONTAINERS[entity.kind]
        for key, value in representations.write(entity.data).items():
            refs = (
                [(containers[key], value)] if key in containers else _references(value)
            )
            for kind, ref in refs:
                if not isinstance(ref, str) or kind in (TABLE, FIELD):
                    continue
                if kind == DATABASE and len(index.databases.get(ref, [])) > 1:
                    problems.append(
                        f"{entity.path}: {key} refers to database {ref!r}, but "
                        f"{len(index.databases[ref])} databases in Metabase have that "
                        "name; rename one of them in Metabase"
                    )
                    continue
                if kind == DATABASE:
                    exists = ref in index.databases
                else:
                    exists = (
                        ref in tree.entities or index.existing_id(kind, ref) is not None
                    )
                if not exists:
                    problems.append(
                        f"{entity.path}: {key} refers to {kind} {ref}, which "
                        "doesn't exist in the files or in Metabase"
                    )
    return problems


def unknown_tables_and_fields(
    tree: LocalTree, remote: Remote, entity_ids: set[str]
) -> list[str]:
    metadata = remote.index.metadata
    lookup = {TABLE: metadata.table_id, FIELD: metadata.field_id}
    problems: list[str] = []
    for eid in sorted(entity_ids):
        entity = tree.get(eid)
        if entity is None:
            continue
        for key, value in representations.write(entity.data).items():
            for kind, ref in _references(value):
                if kind not in lookup or not isinstance(ref, list):
                    continue
                try:
                    lookup[kind](ref)
                except UnresolvedReferenceError as error:
                    problems.append(f"{entity.path}: {key}: {error}")
    return problems


def outside_scope(tree: LocalTree, remote: Remote, scope: set[str] | None) -> list[str]:
    if scope is None:
        return []
    outside: list[str] = []
    for entity in tree.entities.values():
        chain = [entity, *ancestors(tree.entities, entity)]
        if (
            entity.kind == SNIPPET
            or any(e.entity_id in scope for e in chain)
            or any(
                ref in scope or not remote.index.is_outside(ref)
                for ref in container_refs(chain[-1].data)
            )
        ):
            continue
        log.info("outside the collections in %s: %s", CONFIG, entity.path)
        outside.append(str(entity.path))
    if not outside:
        return []
    verb, them = ("is", "it") if len(outside) == 1 else ("are", "them")
    problem = (
        f"{count(len(outside), 'file')} {verb} outside the collections listed under "
        f"`collections:` in {CONFIG}, such as {outside[0]}; move {them} into one of "
        f"those collections, or run `metabase-sync export` to remove {them}"
    )
    return [problem]


def removed_dashcard_filters(tree: LocalTree, remote: Remote) -> list[str]:
    problems: list[str] = []
    for entity in tree.entities.values():
        hit = remote.index.find(entity.entity_id)
        if not isinstance(entity.data, Dashboard) or hit is None or hit[0] != DASHBOARD:
            continue
        theirs = remote.item(*hit)
        if not isinstance(theirs, Dashboard):
            continue
        kept = {dc.entity_id for dc in entity.data.dashcards}
        hosted = {p for dc in entity.data.dashcards for p in dc.inline_parameters or []}
        removed = {
            p
            for dc in theirs.dashcards
            if dc.entity_id not in kept
            for p in dc.inline_parameters or []
            if p not in hosted
        }
        problems += [
            f"{entity.path}: filter {p.get('name') or p['id']!r} sits on a dashcard "
            "you removed, and Metabase removes it with that dashcard; remove the "
            "filter too"
            for p in entity.data.parameters
            if p.get("id") in removed
        ]
    return problems


def _claim(seen: dict[str, str], eid: str, where: str, report: Report) -> None:
    if eid in seen:
        report.errors.append(f"{where}: entity_id {eid} is also used in {seen[eid]}")
    else:
        seen[eid] = where
