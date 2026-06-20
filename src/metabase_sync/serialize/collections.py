"""Collection tree → directory tree mapping.

Each collection gets `<dir>/_collection.yaml`. The directory _is_ the identity;
the YAML stores fields needed to recreate or update.
"""

from __future__ import annotations

from pathlib import Path

from metabase_sync.models import Collection
from metabase_sync.serialize.paths import CollectionPaths
from metabase_sync.serialize.yamlio import load_yaml, write_yaml

_COLLECTION_FILE = "_collection.yaml"
ROOT_SENTINEL = 0  # internal id for the "root" pseudo-collection in path maps


def _is_real_collection(c: Collection) -> bool:
    if c.id == "root":
        return False
    if c.is_personal or c.personal_owner_id is not None:
        return False
    return True


def build_paths(state_dir: Path, collections: list[Collection]) -> CollectionPaths:
    """Walk parents-first so children's parent path is always known.

    Includes a synthetic "root" entry at id=ROOT_SENTINEL so root-level cards and
    dashboards have a parent path to attach to.
    """
    paths = CollectionPaths(state_dir)
    paths.add(ROOT_SENTINEL, None, "_root", None)

    by_id: dict[int, Collection] = {
        c.id: c for c in collections if isinstance(c.id, int)
    }
    visited: set[int] = set()

    def visit(cid: int) -> None:
        if cid in visited:
            return
        c = by_id.get(cid)
        if c is None or not _is_real_collection(c):
            return
        if c.parent_id is not None and c.parent_id in by_id:
            visit(c.parent_id)
        visited.add(cid)
        paths.add(cid, c.parent_id, c.name, c.entity_id)

    for cid in by_id:
        visit(cid)
    return paths


def is_personal(collections: list[Collection], cid: int | None) -> bool:
    if cid is None:
        return False
    for c in collections:
        if c.id == cid:
            return c.is_personal or c.personal_owner_id is not None
    return False


def write_collections(collections: list[Collection], paths: CollectionPaths) -> None:
    for c in collections:
        if not _is_real_collection(c) or not isinstance(c.id, int):
            continue
        directory = paths.directory_for(c.id)
        directory.mkdir(parents=True, exist_ok=True)
        write_yaml(
            directory / _COLLECTION_FILE,
            {
                "entity_id": c.entity_id,
                "name": c.name,
                "description": c.description,
                "authority_level": c.authority_level,
                "archived": c.archived,
            },
        )


def read_collections(state_dir: Path) -> list[tuple[Path, dict]]:
    """Return [(collection_dir, manifest_dict)] in parents-first order."""
    root = state_dir / "collections"
    if not root.exists():
        return []
    out: list[tuple[Path, dict]] = []
    for path in sorted(root.rglob(_COLLECTION_FILE)):
        out.append((path.parent, load_yaml(path)))
    out.sort(key=lambda kv: len(kv[0].relative_to(root).parts))
    return out
