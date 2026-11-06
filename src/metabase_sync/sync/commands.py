from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

from metabase_sync.errors import (
    ApplyStopped,
    BlockedError,
    ConfigError,
    ConflictError,
    MetabaseSyncError,
    NotKeptError,
    StalePlanError,
    StateError,
    ValidationFailed,
    describe,
)
from metabase_sync.format import representations
from metabase_sync.format.api import for_version
from metabase_sync.format.representations import DASHBOARD, KINDS, SUBSCRIPTION
from metabase_sync.ids import EntityId, new_entity_id
from metabase_sync.metabase.client import MetabaseClient, UncertainWriteError
from metabase_sync.metabase.index import (
    LISTINGS,
    RemoteItem,
    add_hidden_subscriptions,
    fetch_index,
    manageable,
    rows,
)
from metabase_sync.metabase.remote import Remote
from metabase_sync.store import tree, yamlio
from metabase_sync.store.journal import Journal
from metabase_sync.store.state import Managed, write_state
from metabase_sync.store.tree import CONFIG, Changes, Entity, LocalTree, read_tree
from metabase_sync.sync.apply import Executor, reconcile
from metabase_sync.sync.compare import not_kept, remote_view
from metabase_sync.sync.match import find_imports, renamed
from metabase_sync.sync.operations import (
    ARCHIVE,
    POSITIVE,
    Operation,
    Plan,
    digest,
    local_hash,
)
from metabase_sync.sync.plan import build_plan
from metabase_sync.sync.validate import Report, validate


class Config(BaseModel):
    collections: set[EntityId] | None = None
    snippets: bool | None = None
    removal: Literal["archive", "delete"] = ARCHIVE
    unmanaged: Literal["keep", "archive", "delete"] = "keep"

    @property
    def manages_snippets(self) -> bool:
        return self.collections is None if self.snippets is None else self.snippets


def load_config(root: Path) -> Config:
    path = root / CONFIG
    try:
        return Config.model_validate(yamlio.read(path) or {}, extra="forbid")
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {yamlio.problem(e)}") from e
    except ValidationError as e:
        raise ConfigError(f"{path}: {'; '.join(describe(e, Config))}") from e


def connect(client: MetabaseClient, config: Config, local: LocalTree) -> Remote:
    client.report("Connecting to Metabase")
    api = for_version(*client.supported_version)
    index = fetch_index(client, config.collections, config.manages_snippets)
    unknown = sorted(
        eid
        for eid in config.collections or ()
        if index.find(eid) is None and eid not in local.entities
    )
    if unknown:
        raise ConfigError(
            f"{CONFIG} lists collections that aren't in Metabase, or are personal: "
            + ", ".join(unknown),
            hint="Run `metabase-sync init --collection NAME` to look up a "
            "collection's entity id",
        )
    add_hidden_subscriptions(client, index, local.ids_of(SUBSCRIPTION))
    return Remote(index=index, api=api)


def find_collections(
    client: MetabaseClient, wanted: list[str]
) -> list[tuple[str, str]]:
    path, query = LISTINGS["collections"]
    listed = [
        RemoteItem.model_validate(row, extra="ignore")
        for row in rows(client.get(path, **query))
    ]
    chains = [chain for chain in manageable(listed).values() if chain[-1].entity_id]
    found: dict[str, str] = {}
    for name in wanted:
        paths = {
            chain[-1].entity_id or "": " / ".join(link.name for link in chain)
            for chain in chains
            if _named(chain[-1], name)
        }
        matches = {eid: where for eid, where in paths.items() if eid == name} or paths
        if not matches and any(_named(c, name) for c in listed):
            raise ConfigError(
                f"Collection {name!r} is a personal or system collection, which "
                "metabase-sync doesn't manage"
            )
        if not matches:
            raise ConfigError(f"No collection has the name or entity id {name!r}")
        if len(matches) > 1:
            listing = "\n".join(f"  {where}  {eid}" for eid, where in matches.items())
            raise ConfigError(
                f"{len(matches)} collections are named {name!r}:\n{listing}",
                hint="Pass the entity id of the one you want to --collection",
            )
        found |= matches
    return list(found.items())


def _named(collection: RemoteItem, name: str) -> bool:
    return collection.entity_id == name or (
        collection.name.strip().casefold() == name.strip().casefold()
    )


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


@dataclass
class ExportResult:
    files: Changes
    overwrites: dict[str, str]
    new: list[str]
    warnings: list[str]
    broken: list[str]
    missing: list[str]
    current: dict[Path, str] = field(repr=False)
    rendered: dict[Path, str] = field(repr=False)
    state: dict[str, Managed] = field(repr=False)
    moves: dict[Path, Path] = field(repr=False)


def _not_interrupted(root: Path) -> None:
    if Journal(root).entries():
        raise StateError(
            "An apply was interrupted",
            hint="Run `metabase-sync apply` to finish it first",
        )


def pull(client: MetabaseClient, root: Path) -> ExportResult:
    tree.require(root)
    _not_interrupted(root)
    current = tree.current_files(root)
    local = read_tree(root)
    if local.problems:
        raise ValidationFailed(local.problems)
    config = load_config(root)
    remote = connect(client, config, local)
    local = renamed(local, find_imports(local, remote)[0])
    pulled = {e.entity_id: e for e in remote_entities(remote)}
    new = [
        e
        for eid, e in local.entities.items()
        if eid not in pulled and eid not in local.state
    ]
    paths = tree.layout([*pulled.values(), *new])
    rendered = {
        paths[eid]: yamlio.dumps(representations.write(e.data))
        for eid, e in pulled.items()
    }
    for e in new:
        path = e.path or paths[e.entity_id]
        if path in rendered:
            raise StateError(
                f"{path} isn't in Metabase yet, but Metabase has another item there",
                hint="Run `metabase-sync apply` first, or move the file",
            )
        if path in current:
            rendered[path] = current[path]
    moves = {
        mine.path: paths[eid]
        for eid in pulled
        if (mine := local.entities.get(eid)) and mine.path
    }
    return ExportResult(
        files=tree.changes(current, rendered, moves),
        overwrites=_overwrites(local, pulled, paths),
        new=sorted(str(e.path or paths[e.entity_id]) for e in new),
        warnings=list(dict.fromkeys(remote.index.warnings)),
        broken=remote.index.broken,
        missing=remote.index.missing,
        current=current,
        rendered=rendered,
        state={
            eid: Managed(kind=e.kind, hash=local_hash(e)) for eid, e in pulled.items()
        },
        moves=moves,
    )


def write(root: Path, result: ExportResult) -> None:
    if tree.current_files(root) != result.current:
        raise StateError(
            "Files changed during export. Nothing was written",
            hint="Run `metabase-sync export` again",
        )
    tree.write_tree(root, result.rendered, result.state, result.moves)


def _overwrites(
    local: LocalTree, pulled: dict[str, Entity], paths: dict[str, Path]
) -> dict[str, str]:
    found: dict[str, str] = {}
    for eid in local.entities.keys() | local.state.keys():
        mine, theirs, prior = local.entities.get(eid), pulled.get(eid), local.prior(eid)
        if mine is None:
            if prior is not None and theirs is not None:
                found[str(paths[eid])] = "restores a file you deleted"
        elif local_hash(mine) != prior:
            if theirs is None and prior is not None and mine.path:
                found[str(mine.path)] = "deletes your edit"
            elif theirs is not None and _differs(theirs, mine):
                found[str(paths[eid])] = "overwrites your edit"
    return found


def _differs(theirs: Entity, mine: Entity) -> bool:
    ours = representations.comparable(mine.data)
    return remote_view(representations.comparable(theirs.data), ours) != ours


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
    notes: list[str] = []
    if not recover:
        _not_interrupted(root)
    elif Journal(root).entries():
        api = for_version(*client.supported_version)
        client.report("Recovering the interrupted apply")
        notes = reconcile(client, api, Journal(root), read_tree(root))
    local = read_tree(root)
    report = validate(local, config.collections)
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
        prepared.tree,
        prepared.remote,
        config.removal,
        config.collections,
        force,
        config.unmanaged,
    )
    return prepared, result


def plan_apply(
    client: MetabaseClient, root: Path, plan_file: Path | None, force: bool = False
) -> tuple[Prepared, Plan]:
    saved = _read_plan(plan_file) if plan_file else None
    if saved is not None:
        force = saved.force
    prepared, result = plan(client, root, force, recover=True)
    if saved is not None and saved.fingerprint != result.fingerprint:
        before = {op.model_dump_json(): op for op in saved.operations}
        after = {op.model_dump_json(): op for op in result.operations}
        both = before | after
        differ = sorted(
            {f"{both[k].kind} {both[k].name!r}" for k in before.keys() ^ after.keys()}
        )
        raise StalePlanError(
            f"{plan_file} no longer matches Metabase or the files"
            + (f"; these items changed: {', '.join(differ)}" if differ else ""),
            hint="Run `metabase-sync plan --out` again and review it",
        )
    if result.blocked:
        raise BlockedError(result.blocked)
    if result.conflicts and not force:
        raise ConflictError(result.conflicts)
    return prepared, result


def apply(
    client: MetabaseClient,
    root: Path,
    prepared: Prepared,
    result: Plan,
    on_done: Callable[[Operation, str], None],
) -> None:
    journal = Journal(root)
    if result.empty:
        write_state(root, _state_after(prepared.tree, result))
        journal.clear()
        return
    executor = Executor(client, prepared.tree, prepared.remote, journal, on_done)
    written = {op.entity_id for op in result.operations if op.action in POSITIVE}
    try:
        executor.run(result)
        written = {executor.renamed.get(eid, eid) for eid in written}
        prepared.remote.refresh(client, written)
    except UncertainWriteError:
        raise
    except Exception as error:
        client.report("Checking what was applied before the error")
        reconcile(client, prepared.remote.api, journal, prepared.tree)
        if isinstance(error, MetabaseSyncError):
            raise ApplyStopped(error, len(executor.applied)) from error
        raise
    dropped = not_kept(prepared.tree, prepared.remote, written)
    state = _state_after(prepared.tree, result)
    for item in dropped:
        hit = prepared.remote.index.find(item.entity_id)
        if hit is not None:
            theirs = representations.comparable(prepared.remote.item(*hit))
            state[item.entity_id] = Managed(kind=item.kind, hash=digest(theirs))
    write_state(root, state)
    journal.clear()
    if dropped:
        raise NotKeptError(dropped)


def _read_plan(path: Path) -> Plan:
    try:
        return Plan.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as e:
        raise ConfigError(
            f"{path} is not a saved plan: {'; '.join(describe(e))}",
            hint="Save one with `metabase-sync plan --out FILE`",
        ) from e


def _state_after(local: LocalTree, result: Plan) -> dict[str, Managed]:
    applied = {op.entity_id for op in result.operations}
    left_out = {c.entity_id for c in result.conflicts} & local.entities.keys() - applied
    state = {
        eid: Managed(kind=entity.kind, hash=local_hash(entity))
        for eid, entity in local.entities.items()
        if eid not in left_out
    }
    return state | {eid: local.state[eid] for eid in left_out if eid in local.state}


def fmt(root: Path) -> tuple[list[str], Changes]:
    tree.require(root)
    _not_interrupted(root)
    local = read_tree(root)
    if local.problems:
        raise ValidationFailed(local.problems)
    entities = list(local.entities.values())
    mapping = {old: new_entity_id() for e in entities for old in e.provisional}
    entities = [
        replace(e, data=representations.swapped(e.data, mapping)) for e in entities
    ]
    paths = tree.layout(entities)
    rendered = tree.render(entities)
    before = tree.current_files(root)
    moves = {e.path: paths[e.entity_id] for e in entities if e.path}
    if rendered != before:
        tree.write_tree(root, rendered, None, moves)
    assigned = sorted(str(e.path) for e in entities if e.provisional)
    return assigned, tree.changes(before, rendered, moves)


@dataclass
class Status:
    interrupted: bool
    report: Report
    plan: Plan | None = None
    warnings: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def status(client: MetabaseClient, root: Path) -> Status:
    tree.require(root)
    local = read_tree(root)
    config = load_config(root)
    result = Status(
        interrupted=bool(Journal(root).entries()),
        report=validate(local, config.collections),
    )
    if result.interrupted or result.report.errors:
        return result
    remote = connect(client, config, local)
    result.plan = build_plan(
        local, remote, config.removal, config.collections, unmanaged=config.unmanaged
    )
    groups: dict[tuple[str, str, int | None], list[RemoteItem]] = {}
    for kind in KINDS:
        for item in remote.index.live(kind):
            key = (kind, item.name.strip().lower(), item.container(kind))
            groups.setdefault(key, []).append(item)
    for (kind, _, _), items in groups.items():
        if len(items) > 1 and kind != SUBSCRIPTION:
            names = sorted({i.name for i in items})
            result.warnings.append(
                f"{len(items)} {kind}s named {names[0]!r} share a collection in Metabase"
            )
    result.warnings += remote.index.warnings
    result.broken = remote.index.broken
    result.missing = remote.index.missing
    return result
