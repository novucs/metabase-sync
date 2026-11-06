from metabase_sync.errors import ValidationFailed
from metabase_sync.format.representations import KINDS
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import LocalTree
from metabase_sync.sync.compare import compare
from metabase_sync.sync.match import find_imports, renamed
from metabase_sync.sync.operations import ARCHIVE, POSITIVE, RESTORE, Plan, Ref
from metabase_sync.sync.order import ordered
from metabase_sync.sync.remove import removals
from metabase_sync.sync.validate import remote_problems, unknown_tables_and_fields


def build_plan(
    tree: LocalTree,
    remote: Remote,
    removal: str = ARCHIVE,
    scope: set[str] | None = None,
    force: bool = False,
) -> Plan:
    renames, ambiguous = find_imports(tree, remote)
    tree = renamed(tree, renames)
    problems = remote_problems(tree, remote, scope)
    if problems:
        raise ValidationFailed(problems)
    compared = compare(tree, remote, ambiguous, force)
    writing = {op.entity_id for op in compared.operations if op.action in POSITIVE}
    problems = unknown_tables_and_fields(tree, remote, writing)
    if problems:
        raise ValidationFailed(problems)
    restores = [op for op in compared.operations if op.action == RESTORE]
    removed = removals(tree, remote, removal, restores, force)
    return Plan(
        metabase=remote.index.version,
        force=force,
        operations=ordered([*compared.operations, *removed.operations], tree, remote),
        renames=renames,
        drift=compared.drift,
        conflicts=[*compared.conflicts, *removed.conflicts],
        diffs=compared.diffs,
        cascaded=removed.cascaded,
        unmanaged=unmanaged(tree, remote),
    )


def unmanaged(tree: LocalTree, remote: Remote) -> list[Ref]:
    return [
        Ref(kind=kind, entity_id=item.entity_id, name=item.name)
        for kind in KINDS
        for item in remote.index.live(kind)
        if item.entity_id
        and item.entity_id not in tree.entities
        and item.entity_id not in tree.state
    ]
