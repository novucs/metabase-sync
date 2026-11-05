from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from metabase_sync.errors import (
    ConfigError,
    ConflictError,
    NotKeptError,
    StalePlanError,
    StateError,
    ValidationFailed,
    describe,
)
from metabase_sync.format import representations
from metabase_sync.format.api import for_version
from metabase_sync.format.representations import DASHBOARD, KINDS, SUBSCRIPTION
from metabase_sync.ids import EntityId, is_entity_id, new_entity_id
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.metabase.index import (
    RemoteItem,
    add_hidden_subscriptions,
    fetch_index,
)
from metabase_sync.metabase.remote import Remote
from metabase_sync.store import tree, yamlio
from metabase_sync.store.journal import Journal
from metabase_sync.store.state import Managed, read_state, write_state
from metabase_sync.store.tree import CONFIG, Entity, LocalTree, read_tree
from metabase_sync.sync.apply import Executor, reconcile
from metabase_sync.sync.compare import not_kept
from metabase_sync.sync.match import find_imports, renamed
from metabase_sync.sync.operations import (
    ARCHIVE,
    CONFLICT,
    OURS,
    POSITIVE,
    SAME,
    THEIRS,
    UNMANAGED,
    Drift,
    Operation,
    Plan,
    classify,
    local_hash,
)
from metabase_sync.sync.plan import build_plan
from metabase_sync.sync.validate import Report, validate


class Config(BaseModel):
    collections: set[EntityId] | None = None
    removal: Literal["archive", "delete"] = ARCHIVE


def load_config(root: Path) -> Config:
    path = root / CONFIG
    try:
        return Config.model_validate(yamlio.read(path) or {}, extra="forbid")
    except ValidationError as e:
        raise ConfigError(f"{path}: {'; '.join(describe(e))}") from e


def connect(client: MetabaseClient, config: Config, local: LocalTree) -> Remote:
    index = fetch_index(client, config.collections)
    add_hidden_subscriptions(client, index, local.ids_of(SUBSCRIPTION))
    return Remote(index=index, api=for_version(index.minor))


def remote_entities(remote: Remote) -> list[Entity]:
    index = remote.index
    live_dashboards = {d.id for d in index.live(DASHBOARD)}
    out: list[Entity] = []
    for kind in KINDS:
        for item in index.live(kind):
            if not item.entity_id or item.document_id:
                continue
            if kind == SUBSCRIPTION and item.dashboard_id not in live_dashboards:
                continue
            out.append(Entity(kind=kind, data=remote.item(kind, item)))
    return out


class ExportResult(BaseModel):
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    kept: list[str] = []
    conflicts: list[Drift] = []
    warnings: list[str] = []

    @property
    def clean(self) -> bool:
        return not (self.added or self.removed or self.changed or self.conflicts)


def _not_interrupted(root: Path) -> None:
    if Journal(root).entries():
        raise StateError(
            "An apply was interrupted",
            hint="Run `metabase-sync apply` to finish it first",
        )


def export(
    client: MetabaseClient, root: Path, check: bool = False, force: bool = False
) -> ExportResult:
    tree.require(root)
    _not_interrupted(root)
    local = read_tree(root)
    if local.problems:
        raise ValidationFailed(local.problems)
    remote = connect(client, load_config(root), local)
    local = renamed(local, find_imports(local, remote)[0])
    pulled = {e.entity_id: e for e in remote_entities(remote)}
    result = ExportResult(warnings=list(dict.fromkeys(remote.index.warnings)))
    taken: list[Entity] = []
    kept: list[Entity] = []
    state: dict[str, Managed] = {}
    for eid in sorted(pulled.keys() | local.entities.keys() | local.managed):
        mine, theirs = local.entities.get(eid), pulled.get(eid)
        what = mine or theirs
        if what is None:
            continue
        situation = classify(
            local.prior(eid),
            local_hash(mine) if mine else None,
            local_hash(theirs) if theirs else None,
        )
        if situation == CONFLICT and not force:
            if mine is None:
                change = "removed here, changed in Metabase"
            elif theirs is None:
                change = "changed here, archived or deleted in Metabase"
            else:
                change = "changed here and in Metabase"
            result.conflicts.append(
                Drift(
                    kind=what.kind,
                    entity_id=eid,
                    name=what.name,
                    path=str(mine.path) if mine else "",
                    change=change,
                )
            )
            continue
        if (
            situation in (SAME, THEIRS, CONFLICT)
            or (situation == UNMANAGED and mine is None)
            or (force and (eid in local.state or theirs is not None))
        ):
            if theirs is not None:
                taken.append(theirs)
                state[eid] = Managed(kind=theirs.kind, hash=local_hash(theirs))
            continue
        if mine is not None:
            kept.append(mine)
        if eid in local.state:
            state[eid] = local.state[eid]
        if situation == OURS or (situation == UNMANAGED and theirs is not None):
            result.kept.append(
                str(mine.path) if mine else f"your removal of {what.kind} {what.name!r}"
            )
    current = tree.current_files(root)
    paths = tree.layout([*taken, *kept])
    rendered = {
        paths[e.entity_id]: yamlio.dumps(representations.write(e.data)) for e in taken
    }
    for e in kept:
        path = e.path or paths[e.entity_id]
        if path in rendered:
            raise StateError(
                f"{path} has unapplied edits and Metabase now puts another item there",
                hint="Run `metabase-sync apply` first, or move the file",
            )
        rendered[path] = current[path]
    result.added = sorted(str(p) for p in rendered.keys() - current.keys())
    result.removed = sorted(str(p) for p in current.keys() - rendered.keys())
    result.changed = sorted(
        str(p) for p in rendered.keys() & current.keys() if rendered[p] != current[p]
    )
    if not check and not result.conflicts:
        tree.write_tree(root, rendered, state)
    return result


@dataclass
class Prepared:
    tree: LocalTree
    remote: Remote
    report: Report
    notes: list[str]
    config: Config


def prepare(client: MetabaseClient, root: Path, recover: bool) -> Prepared:
    tree.require(root)
    config = load_config(root)
    if recover:
        api = for_version(client.supported_version[0])
        notes = reconcile(client, api, Journal(root), read_tree(root))
    else:
        _not_interrupted(root)
        notes = []
    local = read_tree(root)
    report = validate(local)
    if report.errors:
        raise ValidationFailed(report.errors)
    remote = connect(client, config, local)
    return Prepared(
        tree=local, remote=remote, report=report, notes=notes, config=config
    )


def plan(
    client: MetabaseClient, root: Path, force: bool = False, recover: bool = False
) -> tuple[Prepared, Plan]:
    prepared = prepare(client, root, recover)
    config = prepared.config
    result = build_plan(
        prepared.tree, prepared.remote, config.removal, config.collections, force
    )
    return prepared, result


def apply(
    client: MetabaseClient,
    root: Path,
    plan_file: Path | None,
    approve: Callable[[Prepared, Plan], bool],
    on_done: Callable[[Operation, str], None],
    force: bool = False,
) -> tuple[Prepared, Plan, bool]:
    saved = _read_plan(plan_file) if plan_file else None
    if saved is not None:
        force = saved.force
    prepared, result = plan(client, root, force, recover=True)
    if saved is not None and saved.fingerprint != result.fingerprint:
        raise StalePlanError(
            f"{plan_file} no longer matches Metabase or the files",
            hint="Run `metabase-sync plan --out` again and review it",
        )
    if result.conflicts and not force:
        raise ConflictError(result.conflicts)
    if saved is None and not result.empty and not approve(prepared, result):
        return prepared, result, False
    journal = Journal(root)
    if result.empty:
        write_state(root, _state_after(prepared.tree, result))
        journal.clear()
        return prepared, result, True
    executor = Executor(client, prepared.tree, prepared.remote, journal, on_done)
    executor.run(result)
    write_state(root, _state_after(prepared.tree, result))
    journal.clear()
    written = {
        executor.renamed.get(op.entity_id, op.entity_id)
        for op in result.operations
        if op.action in POSITIVE
    }
    prepared.remote.refresh(client, written)
    dropped = not_kept(prepared.tree, prepared.remote, written)
    if dropped:
        raise NotKeptError(dropped)
    return prepared, result, True


def _read_plan(path: Path) -> Plan:
    try:
        return Plan.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as e:
        raise ConfigError(
            f"{path} is not a saved plan: {'; '.join(describe(e))}",
            hint="Save one with `metabase-sync plan --out FILE`",
        ) from e


def _state_after(local: LocalTree, result: Plan) -> dict[str, Managed]:
    drifted = set() if result.force else result.drifted
    state = {
        eid: local.state[eid]
        if eid in drifted and eid in local.state
        else Managed(kind=entity.kind, hash=local_hash(entity))
        for eid, entity in local.entities.items()
    }
    for eid in drifted - state.keys():
        if eid in local.state:
            state[eid] = local.state[eid]
    return state


def fmt(root: Path) -> tuple[list[str], list[str]]:
    tree.require(root)
    assigned: list[str] = []
    found, problems = tree.load_files(root)
    if problems:
        raise ValidationFailed(problems)
    for file in found:
        for holder, _ in tree.id_holders(file.data):
            if not is_entity_id(holder.get("entity_id")):
                holder["entity_id"] = new_entity_id()
                if str(file.path) not in assigned:
                    assigned.append(str(file.path))
    seen: set[str] = set()
    for file in found:
        eid = file.data["entity_id"]
        if eid in seen:
            raise ValidationFailed([f"{file.path}: duplicate entity_id {eid}"])
        seen.add(eid)
    entities: list[Entity] = []
    invalid: list[str] = []
    for file in found:
        try:
            item = representations.read(file.data, file.kind, file.path)
        except ValidationFailed as e:
            invalid.extend(e.problems)
        else:
            entities.append(Entity(kind=file.kind, data=item, path=file.path))
    if invalid:
        raise ValidationFailed(invalid)
    rendered = tree.render(entities)
    before = tree.current_files(root)
    moved = sorted(str(p) for p in rendered.keys() - before.keys())
    if rendered != before:
        tree.write_tree(root, rendered, read_state(root))
    return assigned, moved


class Status(BaseModel):
    interrupted: bool
    report: Report
    plan: Plan | None = None
    duplicates: list[str] = []


def status(client: MetabaseClient, root: Path) -> Status:
    tree.require(root)
    local = read_tree(root)
    result = Status(interrupted=bool(Journal(root).entries()), report=validate(local))
    if result.interrupted or result.report.errors:
        return result
    config = load_config(root)
    remote = connect(client, config, local)
    result.plan = build_plan(local, remote, config.removal, config.collections)
    groups: dict[tuple[str, str, int | None], list[RemoteItem]] = {}
    for kind in KINDS:
        for item in remote.index.live(kind):
            key = (kind, item.name.strip().lower(), item.container(kind))
            groups.setdefault(key, []).append(item)
    for (kind, _, _), items in groups.items():
        if len(items) > 1 and kind != SUBSCRIPTION:
            names = sorted({i.name for i in items})
            result.duplicates.append(
                f"{len(items)} {kind}s named {names[0]!r} share a collection in Metabase"
            )
    return result
