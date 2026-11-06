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
from metabase_sync.sync.compare import not_kept, remote_hash
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
    kept: list[str]
    conflicts: list[Drift]
    warnings: list[str]
    broken: list[str]
    missing: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.files.empty and not self.conflicts


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
    config = load_config(root)
    remote = connect(client, config, local)
    local = renamed(local, find_imports(local, remote)[0])
    pulled = {e.entity_id: e for e in remote_entities(remote)}
    conflicts: list[Drift] = []
    unapplied: list[str] = []
    taken: list[Entity] = []
    kept: list[Entity] = []
    state: dict[str, Managed] = {}
    for eid in sorted(pulled.keys() | local.entities.keys() | local.state.keys()):
        mine, theirs = local.entities.get(eid), pulled.get(eid)
        what = mine or theirs
        if what is None:
            continue
        prior = local.prior(eid)
        ours = representations.comparable(mine.data) if mine else {}
        situation = classify(
            prior,
            digest(ours) if mine else None,
            remote_hash(prior, representations.comparable(theirs.data), ours)
            if theirs
            else None,
        )
        if situation == CONFLICT and not force:
            if mine is None:
                change = "removed here, changed in Metabase"
            elif theirs is None:
                change = "changed here, archived or deleted in Metabase"
            else:
                change = "changed here and in Metabase"
            conflicts.append(
                Drift(
                    kind=what.kind,
                    entity_id=eid,
                    name=what.name,
                    path=str(mine.path) if mine else None,
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
            unapplied.append(
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
    moves = {
        mine.path: paths[e.entity_id]
        for e in taken
        if (mine := local.entities.get(e.entity_id)) and mine.path
    }
    if not check and not conflicts:
        tree.write_tree(root, rendered, state, moves)
    return ExportResult(
        files=tree.changes(current, rendered, moves),
        kept=unapplied,
        conflicts=conflicts,
        warnings=list(dict.fromkeys(remote.index.warnings)),
        broken=remote.index.broken,
        missing=remote.index.missing,
    )


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
        raise ConflictError(result.conflicts, len(result.drift))
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
