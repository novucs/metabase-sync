import base64
import hashlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify as python_slugify

from metabase_sync.errors import ConfigError, ValidationFailed
from metabase_sync.format import representations
from metabase_sync.format.representations import MODELS, Item
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.snippet import Snippet
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.store import yamlio
from metabase_sync.store.state import Managed, read_state, write_state

MAIN = Path("collections") / "main"
SNIPPETS = Path("collections") / "snippets"
SUBSCRIPTIONS = Path("subscriptions")
WORKDIR = ".metabase-sync"
CONFIG = "metabase-sync.yaml"
CONTENT_ROOTS = (Path("collections"), SUBSCRIPTIONS)


@dataclass
class File:
    path: Path
    kind: str
    data: dict[str, Any]


@dataclass
class Entity:
    kind: str
    data: Item
    path: Path | None = None
    provisional: list[str] = field(default_factory=list)

    @property
    def entity_id(self) -> str:
        return self.data.entity_id

    @property
    def name(self) -> str:
        return "" if isinstance(self.data, Subscription) else self.data.name


@dataclass
class LocalTree:
    root: Path
    entities: dict[str, Entity] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    state: dict[str, Managed] = field(default_factory=dict)

    def prior(self, entity_id: str) -> str | None:
        found = self.state.get(entity_id)
        return found.hash if found else None

    def ids_of(self, kind: str) -> set[str]:
        found = {eid for eid, e in self.entities.items() if e.kind == kind}
        return found | {eid for eid, entry in self.state.items() if entry.kind == kind}

    def get(self, entity_id: str | None) -> Entity | None:
        return self.entities.get(entity_id) if entity_id is not None else None

    def label(self, entity: Entity) -> str:
        if not isinstance(entity.data, Subscription):
            return entity.name
        dashboard = self.get(entity.data.dashboard_id)
        return f"to {dashboard.name}" if dashboard else "to an unmanaged dashboard"


def container_refs(item: Item) -> list[str]:
    if isinstance(item, Subscription):
        refs = [item.dashboard_id]
    elif isinstance(item, Card):
        refs = [item.dashboard_id, item.collection_id]
    else:
        refs = [representations.collection_of(item)]
    return [ref for ref in refs if ref]


def parent(entities: Mapping[str, Entity], entity: Entity) -> Entity | None:
    return next(
        (entities[ref] for ref in container_refs(entity.data) if ref in entities),
        None,
    )


def ancestors(entities: Mapping[str, Entity], entity: Entity) -> list[Entity]:
    found: list[Entity] = []
    seen = {entity.entity_id}
    cursor = parent(entities, entity)
    while cursor is not None:
        if cursor.entity_id in seen:
            raise ValidationFailed(
                [f"{entity.path}: {cursor.kind} {cursor.entity_id} is inside itself"]
            )
        seen.add(cursor.entity_id)
        found.append(cursor)
        cursor = parent(entities, cursor)
    return found


def load_files(root: Path) -> tuple[list[File], list[str]]:
    found: list[File] = []
    problems: list[str] = []
    for base in CONTENT_ROOTS:
        for path in sorted((root / base).rglob("*.yaml")):
            rel = path.relative_to(root)
            try:
                data = yamlio.read(path)
            except yaml.YAMLError as e:
                problems.append(f"{rel}: {yamlio.problem(e)}")
                continue
            kind = representations.kind_of(data) if isinstance(data, dict) else None
            if not isinstance(data, dict):
                problems.append(f"{rel}: expected a mapping")
            elif kind is None:
                problems.append(
                    f"{rel}: serdes/meta needs a model, one of {', '.join(MODELS)}"
                )
            else:
                found.append(File(path=rel, kind=kind, data=data))
    return found, problems


def require(root: Path) -> None:
    if not (root / CONFIG).exists():
        raise ConfigError(
            f"{root.resolve()} has no {CONFIG}",
            hint="Run `metabase-sync init` there, or pass --chdir DIR to run elsewhere",
        )


def provisional_id(*parts: object) -> str:
    seed = ":".join(str(p) for p in parts).encode()
    return base64.urlsafe_b64encode(hashlib.sha256(seed).digest()).decode()[:21]


def id_holders(
    data: dict[str, Any],
) -> Iterator[tuple[dict[str, Any], tuple[str | int, ...]]]:
    yield data, ()
    for key in ("tabs", "dashcards"):
        for index, nested in enumerate(data.get(key) or []):
            if isinstance(nested, dict):
                yield nested, (key, index)


def _fill_ids(file: File) -> list[str]:
    filled: list[str] = []
    for holder, at in id_holders(file.data):
        if holder.get("entity_id") is None:
            holder["entity_id"] = provisional_id(file.path, *at)
            filled.append(holder["entity_id"])
    return filled


def read_tree(root: Path) -> LocalTree:
    tree = LocalTree(root=root)
    if not root.exists():
        return tree
    found, tree.problems = load_files(root)
    for file in found:
        provisional = _fill_ids(file)
        eid = file.data.get("entity_id")
        if isinstance(eid, str) and eid in tree.entities:
            tree.problems.append(
                f"{file.path}: entity_id {eid} already used by "
                f"{tree.entities[eid].path}; remove one of the two files, or remove "
                "entity_id from a copy to make it a new item"
            )
            continue
        try:
            item = representations.read(file.data, file.kind, file.path)
        except ValidationFailed as e:
            tree.problems.extend(e.problems)
        else:
            tree.entities[item.entity_id] = Entity(
                kind=file.kind, data=item, path=file.path, provisional=provisional
            )
    tree.state = read_state(root)
    return tree


def slugify(name: str) -> str:
    return python_slugify(name or "", separator="_", max_length=60) or "untitled"


@dataclass(frozen=True)
class Named:
    entity_id: str
    slug: str


def assign_names(items: list[Named]) -> dict[str, str]:
    by_slug: dict[str, list[Named]] = {}
    for item in items:
        by_slug.setdefault(item.slug.lower(), []).append(item)
    names: dict[str, str] = {}
    for group in by_slug.values():
        if len(group) == 1:
            names[group[0].entity_id] = group[0].slug
            continue
        for width in (10, 21):
            candidates = {
                i.entity_id: f"{i.slug}__{i.entity_id[:width].lower()}" for i in group
            }
            if len({c.lower() for c in candidates.values()}) == len(group):
                names.update(candidates)
                break
        else:
            for index, item in enumerate(sorted(group, key=lambda i: i.entity_id)):
                names[item.entity_id] = f"{item.slug}__{index}"
    return names


def layout(entities: list[Entity]) -> dict[str, Path]:
    by_id = {e.entity_id: e for e in entities}
    special = {Snippet: SNIPPETS, Subscription: SUBSCRIPTIONS}

    def place(entity: Entity) -> Path | str | None:
        if type(entity.data) in special:
            return special[type(entity.data)]
        found = parent(by_id, entity)
        return found.entity_id if found else None

    siblings: dict[Path | str | None, list[Named]] = {}
    for entity in entities:
        subscribed = (
            entity.data.dashboard_id if isinstance(entity.data, Subscription) else None
        )
        source = by_id[subscribed].name if subscribed in by_id else entity.name
        siblings.setdefault(place(entity), []).append(
            Named(entity_id=entity.entity_id, slug=slugify(source))
        )
    names: dict[str, str] = {}
    for group in siblings.values():
        names.update(assign_names(group))

    def path(entity: Entity) -> Path:
        file = f"{names[entity.entity_id]}.yaml"
        if type(entity.data) in special:
            return special[type(entity.data)] / file
        folders = [names[a.entity_id] for a in reversed(ancestors(by_id, entity))]
        return MAIN.joinpath(*folders, file)

    return {e.entity_id: path(e) for e in entities}


def render(entities: list[Entity]) -> dict[Path, str]:
    paths = layout(entities)
    return {
        paths[e.entity_id]: yamlio.dumps(representations.write(e.data))
        for e in entities
    }


@dataclass
class Changes:
    added: list[str]
    changed: list[str]
    moved: list[str]
    removed: list[str]

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.moved or self.removed)


def changes(
    current: dict[Path, str], rendered: dict[Path, str], moves: dict[Path, Path]
) -> Changes:
    moved = {
        old: new
        for old, new in moves.items()
        if old != new and old in current and new not in current
    }
    return Changes(
        added=sorted(
            str(p) for p in rendered.keys() - current.keys() - set(moved.values())
        ),
        changed=sorted(
            str(p)
            for p in rendered.keys() & current.keys()
            if rendered[p] != current[p]
        ),
        moved=sorted(f"{old} -> {new}" for old, new in moved.items()),
        removed=sorted(str(p) for p in current.keys() - rendered.keys() - moved.keys()),
    )


def current_files(root: Path) -> dict[Path, str]:
    found: dict[Path, str] = {}
    for base in CONTENT_ROOTS:
        for path in sorted((root / base).rglob("*.yaml")):
            found[path.relative_to(root)] = path.read_text(encoding="utf-8")
    return found


def write_tree(
    root: Path,
    rendered: dict[Path, str],
    state: dict[str, Managed] | None,
    moves: dict[Path, Path],
) -> None:
    current = current_files(root)
    for old, new in moves.items():
        if old in current and new not in current:
            (root / new).parent.mkdir(parents=True, exist_ok=True)
            os.replace(root / old, root / new)
            current[new] = current.pop(old)
    for rel, text in rendered.items():
        if current.get(rel) != text:
            yamlio.write_text_atomic(root / rel, text)
    for rel in sorted(current.keys() - rendered.keys()):
        (root / rel).unlink()
        folder = (root / rel).parent
        while folder.parent != root and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
    if state is not None:
        write_state(root, state)
