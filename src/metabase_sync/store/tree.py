import base64
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify as python_slugify

from metabase_sync.errors import ConfigError, StateError, ValidationFailed
from metabase_sync.format import representations
from metabase_sync.format.representations import COLLECTION, DASHBOARD, Item
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.snippet import Snippet
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.ids import is_entity_id
from metabase_sync.store import yamlio
from metabase_sync.store.state import STATE, Managed, read_state, state_text

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
    needs_ids: bool = False

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

    @property
    def managed(self) -> set[str]:
        return set(self.state)

    def prior(self, entity_id: str) -> str | None:
        found = self.state.get(entity_id)
        return found.hash if found else None

    def ids_of(self, kind: str) -> set[str]:
        found = {eid for eid, e in self.entities.items() if e.kind == kind}
        return found | {eid for eid, entry in self.state.items() if entry.kind == kind}

    def get(self, entity_id: object) -> Entity | None:
        return self.entities.get(entity_id) if isinstance(entity_id, str) else None

    def label(self, entity: Entity) -> str:
        if not isinstance(entity.data, Subscription):
            return entity.name
        dashboard = self.get(entity.data.dashboard_id)
        return f"to {dashboard.name}" if dashboard else "to an unmanaged dashboard"


def load_files(root: Path) -> tuple[list[File], list[str]]:
    found: list[File] = []
    problems: list[str] = []
    for base in CONTENT_ROOTS:
        for path in sorted((root / base).rglob("*.yaml")):
            rel = path.relative_to(root)
            try:
                data = yamlio.read(path)
            except yaml.YAMLError as e:
                problems.append(f"{rel}: invalid YAML ({e})")
                continue
            kind = representations.kind_of(data) if isinstance(data, dict) else None
            if not isinstance(data, dict):
                problems.append(f"{rel}: expected a mapping")
            elif kind is None:
                problems.append(f"{rel}: missing or unknown serdes/meta model")
            else:
                found.append(File(path=rel, kind=kind, data=data))
    return found, problems


def require(root: Path) -> None:
    if not (root / CONFIG).exists():
        raise ConfigError(
            f"{root} has no {CONFIG}",
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


def _fill_ids(file: File) -> bool:
    filled = False
    for holder, at in id_holders(file.data):
        if holder.get("entity_id") is None:
            holder["entity_id"] = provisional_id(file.path, *at)
            filled = True
    return filled


def read_tree(root: Path) -> LocalTree:
    tree = LocalTree(root=root)
    if not root.exists():
        return tree
    found, tree.problems = load_files(root)
    for file in found:
        needs_ids = _fill_ids(file)
        eid = file.data.get("entity_id")
        if is_entity_id(eid) and eid in tree.entities:
            tree.problems.append(
                f"{file.path}: entity_id {eid} already used by {tree.entities[eid].path}"
            )
            continue
        try:
            item = representations.read(file.data, file.kind, file.path)
        except ValidationFailed as e:
            tree.problems.extend(e.problems)
        else:
            tree.entities[item.entity_id] = Entity(
                kind=file.kind, data=item, path=file.path, needs_ids=needs_ids
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
    collections = {e.entity_id for e in entities if e.kind == COLLECTION}
    dashboards = {e.entity_id for e in entities if e.kind == DASHBOARD}
    folder_of: dict[str, tuple[str, str | None]] = {}
    for entity in entities:
        eid, data = entity.entity_id, entity.data
        container = representations.collection_of(data)
        if isinstance(data, Card) and data.dashboard_id in dashboards:
            folder_of[eid] = ("dash", data.dashboard_id)
        elif isinstance(data, Snippet):
            folder_of[eid] = ("snippets", None)
        elif isinstance(data, Subscription):
            folder_of[eid] = ("subscriptions", None)
        else:
            folder_of[eid] = ("coll", container if container in collections else None)

    siblings: dict[tuple[str, str | None], list[Named]] = {}
    by_id = {e.entity_id: e for e in entities}
    for entity in entities:
        subscribed = (
            entity.data.dashboard_id if isinstance(entity.data, Subscription) else None
        )
        source = by_id[subscribed].name if subscribed in by_id else entity.name
        siblings.setdefault(folder_of[entity.entity_id], []).append(
            Named(entity_id=entity.entity_id, slug=slugify(source))
        )
    names: dict[str, str] = {}
    for group in siblings.values():
        names.update(assign_names(group))

    fixed: dict[tuple[str, str | None], Path] = {
        ("coll", None): MAIN,
        ("snippets", None): SNIPPETS,
        ("subscriptions", None): SUBSCRIPTIONS,
    }
    resolved: dict[tuple[str, str | None], Path] = {}

    def folder_path(key: tuple[str, str | None], depth: int = 0) -> Path:
        if key in fixed:
            return fixed[key]
        if key in resolved:
            return resolved[key]
        owner = key[1]
        assert owner is not None
        if depth > 100:
            raise StateError(f"collection {owner} is its own ancestor")
        resolved[key] = folder_path(folder_of[owner], depth + 1) / names[owner]
        return resolved[key]

    return {
        e.entity_id: folder_path(folder_of[e.entity_id]) / f"{names[e.entity_id]}.yaml"
        for e in entities
    }


def render(entities: list[Entity]) -> dict[Path, str]:
    paths = layout(entities)
    return {
        paths[e.entity_id]: yamlio.dumps(representations.write(e.data))
        for e in entities
    }


def current_files(root: Path) -> dict[Path, str]:
    found: dict[Path, str] = {}
    for base in CONTENT_ROOTS:
        for path in sorted((root / base).rglob("*.yaml")):
            found[path.relative_to(root)] = path.read_text(encoding="utf-8")
    return found


def write_tree(
    root: Path, rendered: dict[Path, str], state: dict[str, Managed]
) -> None:
    current = current_files(root)
    for rel, text in rendered.items():
        if current.get(rel) != text:
            yamlio.write_text_atomic(root / rel, text)
    for rel in sorted(current.keys() - rendered.keys()):
        (root / rel).unlink()
        folder = (root / rel).parent
        while folder.parent != root and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
    yamlio.write_text_atomic(root / STATE, state_text(state))
