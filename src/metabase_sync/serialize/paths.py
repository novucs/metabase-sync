"""Filesystem layout + slug generation.

Layout under state_dir:

    databases/<db-name>.yaml
    snippets/<snippet-slug>.sql
    collections/<top-slug>/_collection.yaml
                          /<nested-slug>/_collection.yaml
                                        /cards/<card-slug>.sql   (native)
                                              /<card-slug>.yaml  (GUI)
                                        /dashboards/<dash-slug>/dashboard.yaml
                                                              /cards/<card-slug>.sql
    pulses/<pulse-slug>.yaml

Slugs are produced by `python-slugify` (Unicode-aware), bounded to 80 chars,
with a collision suffix derived from the entity_id when two siblings collide.
"""

from __future__ import annotations

import re
from pathlib import Path

from slugify import slugify as _slugify_unicode

_MAX_SLUG = 80
# Strip anything python-slugify left that isn't ASCII alnum/hyphen — used to
# sanitise the entity_id collision suffix into the same alphabet.
_SUFFIX_FILTER_RE = re.compile(r"[^a-z0-9]")


def slugify(name: str) -> str:
    """Lowercase ASCII slug. Falls back to 'unnamed' if the input collapses to
    empty (e.g. an emoji-only name)."""
    s = _slugify_unicode(name, max_length=_MAX_SLUG, lowercase=True, separator="-")
    if not s:
        return "unnamed"
    return s.rstrip("-")


def disambiguate(slug: str, entity_id: str | None, taken: set[str]) -> str:
    if slug not in taken:
        return slug
    raw_suffix = (entity_id or "x")[:6].lower()
    suffix = _SUFFIX_FILTER_RE.sub("", raw_suffix) or "x"
    candidate = f"{slug}-{suffix}"
    n = 1
    while candidate in taken:
        n += 1
        candidate = f"{slug}-{suffix}-{n}"
    return candidate


class CollectionPaths:
    """Maps Metabase collection ids to filesystem path segments.

    Built bottom-up from a flat collection list with `parent_id` links. Personal
    collections (`personal_owner_id is not None`) and the "root" pseudo-collection
    are intentionally excluded from the path map.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir
        self._parent_by_id: dict[int, int | None] = {}
        self._path_by_id: dict[int, Path] = {}
        self._siblings_by_parent: dict[int | None, set[str]] = {}

    def add(
        self, cid: int, parent_id: int | None, name: str, entity_id: str | None
    ) -> Path:
        parent_path = self._path_by_id.get(parent_id) if parent_id is not None else None
        siblings = self._siblings_by_parent.setdefault(parent_id, set())
        segment = disambiguate(slugify(name), entity_id, siblings)
        siblings.add(segment)
        self._parent_by_id[cid] = parent_id
        directory = (parent_path or self.collections_root()) / segment
        self._path_by_id[cid] = directory
        return directory

    def directory_for(self, cid: int) -> Path:
        return self._path_by_id[cid]

    def known(self, cid: int) -> bool:
        return cid in self._path_by_id

    def collections_root(self) -> Path:
        return self._state_dir / "collections"

    def relpath_from_collections(self, cid: int) -> str:
        return str(self._path_by_id[cid].relative_to(self.collections_root()))

    def all_ids(self) -> list[int]:
        return list(self._path_by_id.keys())


def state_subdirs(state_dir: Path) -> dict[str, Path]:
    return {
        "databases": state_dir / "databases",
        "snippets": state_dir / "snippets",
        "collections": state_dir / "collections",
        "pulses": state_dir / "pulses",
    }


def card_filename(slug: str, *, is_native: bool) -> str:
    return f"{slug}.sql" if is_native else f"{slug}.yaml"
