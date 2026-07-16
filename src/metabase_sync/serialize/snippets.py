"""Snippet ↔ .sql file (YAML frontmatter + raw SQL body).

Routing:

* Snippets without a collection live flat under `state/snippets/<slug>.sql`.
* Snippets inside a collection live under
  `state/collections/<...>/snippets/<slug>.sql`. The directory _is_ the routing
  signal — `_collection.yaml` next to it (`../`) identifies the owning
  collection. The frontmatter no longer carries `collection_slug` for these.

read_snippets walks both locations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metabase_sync.models import Snippet
from metabase_sync.serialize.canon import SNIPPET, canonical
from metabase_sync.serialize.paths import CollectionPaths, disambiguate, slugify
from metabase_sync.serialize.yamlio import read_frontmatter_sql, write_frontmatter_sql


def write_snippets(
    state_dir: Path, snippets: list[Snippet], collection_paths: CollectionPaths
) -> None:
    flat_out = state_dir / "snippets"
    flat_out.mkdir(parents=True, exist_ok=True)
    taken_by_dir: dict[Path, set[str]] = {}
    seen_files: set[Path] = set()

    for s in snippets:
        parent_dir = (
            collection_paths.directory_for(s.collection_id) / "snippets"
            if s.collection_id is not None and collection_paths.known(s.collection_id)
            else flat_out
        )
        parent_dir.mkdir(parents=True, exist_ok=True)
        taken = taken_by_dir.setdefault(parent_dir, set())
        slug = disambiguate(slugify(s.name), s.entity_id, taken)
        taken.add(slug)
        frontmatter: dict[str, Any] = {
            "entity_id": s.entity_id,
            "name": s.name,
            "description": s.description,
            "template_tags": s.template_tags or {},
            "archived": s.archived,
        }
        filename = f"{slug}.sql"
        path = parent_dir / filename
        seen_files.add(path)
        write_frontmatter_sql(path, canonical(frontmatter, SNIPPET), s.content)

    # Tidy: remove flat snippet files no longer in the export. (Snippets nested
    # under `collections/.../snippets/` are pruned by the collections wipe in
    # export.py.)
    for existing in flat_out.glob("*.sql"):
        if existing not in seen_files:
            existing.unlink()


def read_snippets(state_dir: Path) -> list[tuple[Path, dict, str]]:
    """Return [(path, frontmatter, body)] for every snippet on disk, whether
    in the flat `state/snippets/` dir or nested under a collection."""
    items: list[tuple[Path, dict, str]] = []

    flat_out = state_dir / "snippets"
    if flat_out.exists():
        for path in sorted(flat_out.glob("*.sql")):
            fm, body = read_frontmatter_sql(path)
            items.append((path, fm, body))

    collections_root = state_dir / "collections"
    if collections_root.exists():
        for path in sorted(collections_root.rglob("snippets/*.sql")):
            fm, body = read_frontmatter_sql(path)
            items.append((path, fm, body))

    return items


def resolve_snippet_collection_dir(state_dir: Path, snippet_path: Path) -> Path | None:
    """Return the collection directory that owns this snippet, or None if it
    lives in the flat top-level snippets folder. Used by apply to look up the
    collection_id."""
    try:
        snippet_path.relative_to(state_dir / "collections")
    except ValueError:
        return None
    # Path layout: collections/<...>/snippets/<slug>.sql → owning collection is two up.
    return snippet_path.parent.parent
