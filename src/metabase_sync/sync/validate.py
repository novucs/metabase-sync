from pydantic import BaseModel

from metabase_sync.format.representations import (
    CONTAINERS,
    DASHBOARD,
    SNIPPET,
    SUBSCRIPTION,
    collection_of,
)
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import CONFIG, Entity, LocalTree, layout


class Report(BaseModel):
    errors: list[str] = []
    warnings: list[str] = []


def validate(tree: LocalTree) -> Report:
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
                if ref not in tree.managed:
                    report.warnings.append(
                        f"{where}: {key} {ref} is not in the files (it must already exist in Metabase)"
                    )
                else:
                    report.errors.append(
                        f"{where}: {key} {ref} was removed from the files"
                    )
            elif target.kind != kind:
                report.errors.append(
                    f"{where}: {key} {ref} is a {target.kind}, expected a {kind}"
                )
        if isinstance(entity.data, Dashboard):
            for nested in [*entity.data.tabs, *entity.data.dashcards]:
                _claim(nested_ids, nested.entity_id, where, report)
            for dc in entity.data.dashcards:
                card = dc.card_id
                if (
                    isinstance(card, str)
                    and tree.get(card) is None
                    and card in tree.managed
                ):
                    report.errors.append(
                        f"{where}: the dashcard at row {dc.row}, column {dc.col} "
                        "shows a question whose file you removed; remove the dashcard too, "
                        "or restore the question's file"
                    )
        container = collection_of(entity.data)
        key = (
            entity.kind,
            container if isinstance(container, str) else None,
            entity.name.strip().lower(),
        )
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


def outside_scope(tree: LocalTree, remote: Remote, scope: set[str] | None) -> list[str]:
    if scope is None:
        return []
    inside: dict[str, bool] = {}

    def contained(ref: str | None) -> bool:
        if ref is None:
            return False
        if ref not in inside:
            inside[ref] = False
            entity = tree.get(ref)
            inside[ref] = (
                ref in scope
                or (entity is not None and contained(_container(entity)))
                or (entity is None and remote.index.find(ref) is not None)
            )
        return inside[ref]

    return [
        f"{entity.path}: this {entity.kind} is outside the collections listed "
        f"under `collections:` in {CONFIG}; move it into one of them, or list "
        "its collection there"
        for entity in tree.entities.values()
        if entity.kind != SNIPPET
        and entity.entity_id not in scope
        and not contained(_container(entity))
    ]


def _container(entity: Entity) -> str | None:
    if isinstance(entity.data, Subscription):
        return entity.data.dashboard_id
    return collection_of(entity.data)


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
