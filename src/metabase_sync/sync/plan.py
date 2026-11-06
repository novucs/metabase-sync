from metabase_sync.errors import ValidationFailed
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import LocalTree
from metabase_sync.sync.compare import compare
from metabase_sync.sync.match import find_imports, renamed
from metabase_sync.sync.operations import ARCHIVE, POSITIVE, RESTORE, Plan
from metabase_sync.sync.order import ordered
from metabase_sync.sync.remove import KEEP, removals
from metabase_sync.sync.validate import remote_problems, unknown_tables_and_fields


def build_plan(
    tree: LocalTree,
    remote: Remote,
    removal: str = ARCHIVE,
    scope: set[str] | None = None,
    force: bool = False,
    unmanaged: str = KEEP,
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
    removed = removals(
        tree,
        remote,
        removal,
        restores,
        force,
        unmanaged,
        whole_instance=scope is None,
        writing=writing,
    )
    return Plan(
        metabase=remote.index.version,
        force=force,
        operations=ordered([*compared.operations, *removed.operations], tree, remote),
        renames=renames,
        drift=compared.drift,
        conflicts=[*compared.conflicts, *removed.conflicts],
        blocked=removed.blocked,
        diffs=compared.diffs,
        cascaded=removed.cascaded,
        unmanaged=removed.unmanaged,
        kept=removed.kept,
    )
