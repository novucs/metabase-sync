"""Author desired on-disk state for integration tests.

These helpers write the same YAML / frontmatter that `metabase-sync export`
produces — they call the *production* serializers
(`metabase_sync.serialize.yamlio`) rather than hand-built strings, so a format
change in the tool automatically flows into the test authoring path and the
builders can never drift from reality.

Every function returns the `Path` it wrote, so tests can read it back to assert
write-back (e.g. entity_id getting filled in after apply).

`coll_slug` is a directory path under `state/collections/` and may contain `/`
for nested collections (e.g. ``"parent/child"``). Use ``"root"`` for the
synthetic root collection that holds items with no Metabase collection.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from metabase_sync.serialize.yamlio import write_frontmatter_sql, write_yaml

_name_counter = itertools.count(1)


def unique_name(prefix: str) -> str:
    """Monotonic, collision-free name for objects on the shared Metabase.
    Deterministic within a session (no Date.now / random)."""
    return f"{prefix}-{next(_name_counter)}"


# Default frontmatter every card carries (mirrors serialize.cards._common_frontmatter).
_CARD_DEFAULTS: dict[str, Any] = {
    "entity_id": None,
    "description": None,
    "type": "question",
    "display": "scalar",
    "parameters": [],
    "visualization_settings": {},
    "enable_embedding": False,
    "embedding_params": None,
    "cache_ttl": None,
    "archived": False,
}

_DASHCARD_DEFAULTS: dict[str, Any] = {
    "entity_id": None,
    "card_path": None,
    "tab_position": None,
    "row": 0,
    "col": 0,
    "size_x": 12,
    "size_y": 4,
    "parameter_mappings": [],
    "visualization_settings": {},
    "series": [],
}


def write_collection(
    state_dir: Path,
    coll_slug: str,
    name: str,
    *,
    entity_id: str | None = None,
    description: str | None = None,
    authority_level: str | None = None,
    archived: bool = False,
) -> Path:
    directory = state_dir / "collections" / coll_slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "_collection.yaml"
    write_yaml(
        path,
        {
            "entity_id": entity_id,
            "name": name,
            "description": description,
            "authority_level": authority_level,
            "archived": archived,
        },
    )
    return path


def write_native_card(
    state_dir: Path,
    coll_slug: str,
    slug: str,
    name: str,
    sql: str,
    database: str,
    *,
    template_tags: dict[str, Any] | None = None,
    **overrides: Any,
) -> Path:
    fm = _CARD_DEFAULTS | {
        "name": name,
        "database": database,
        "template_tags": template_tags or {},
    }
    fm.update(overrides)
    # result_metadata (models) sits in frontmatter after template_tags if present.
    path = state_dir / "collections" / coll_slug / "cards" / f"{slug}.sql"
    write_frontmatter_sql(path, fm, sql)
    return path


def write_gui_card(
    state_dir: Path,
    coll_slug: str,
    slug: str,
    name: str,
    dataset_query: dict[str, Any],
    database: str,
    *,
    display: str = "table",
    **overrides: Any,
) -> Path:
    fm = _CARD_DEFAULTS | {"name": name, "database": database, "display": display}
    fm.update(overrides)
    path = state_dir / "collections" / coll_slug / "cards" / f"{slug}.yaml"
    write_yaml(path, fm | {"dataset_query": dataset_query})
    return path


def write_dashboard(
    state_dir: Path,
    coll_slug: str,
    slug: str,
    name: str,
    *,
    tabs: list[dict[str, Any]] | None = None,
    dashcards: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> Path:
    directory = state_dir / "collections" / coll_slug / "dashboards" / slug
    directory.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "entity_id": None,
        "name": name,
        "description": None,
        "archived": False,
        "auto_apply_filters": True,
        "cache_ttl": None,
        "enable_embedding": False,
        "embedding_params": None,
        "position": None,
        "width": "fixed",
        "parameters": [],
        "tabs": [_tab(t) for t in (tabs or [])],
        "dashcards": [_dashcard(dc) for dc in (dashcards or [])],
    }
    doc.update(overrides)
    path = directory / "dashboard.yaml"
    write_yaml(path, doc)
    return path


def dashcard_to(card_file: str, **fields: Any) -> dict[str, Any]:
    """Build a dashcard dict pointing at a card in the same collection's
    ``cards/`` dir (``card_file`` like ``"hello.sql"``). The path is relative to
    the dashboard directory (``../../cards/<file>``)."""
    return {"card_path": f"../../cards/{card_file}", **fields}


def virtual_text_dashcard(text: str, **fields: Any) -> dict[str, Any]:
    """A text-widget dashcard: card_id is null, the discriminator lives in
    visualization_settings."""
    return {
        "card_path": None,
        "visualization_settings": {
            "virtual_card": {
                "name": None,
                "display": "text",
                "visualization_settings": {},
                "dataset_query": {},
                "archived": False,
            },
            "text": text,
        },
        **fields,
    }


def write_snippet(
    state_dir: Path,
    slug: str,
    name: str,
    body: str,
    *,
    coll_slug: str | None = None,
    entity_id: str | None = None,
    template_tags: dict[str, Any] | None = None,
    archived: bool = False,
) -> Path:
    if coll_slug is None:
        path = state_dir / "snippets" / f"{slug}.sql"
    else:
        path = state_dir / "collections" / coll_slug / "snippets" / f"{slug}.sql"
    write_frontmatter_sql(
        path,
        {
            "entity_id": entity_id,
            "name": name,
            "description": None,
            "template_tags": template_tags or {},
            "archived": archived,
        },
        body,
    )
    return path


def write_pulse(
    state_dir: Path,
    slug: str,
    name: str,
    *,
    dashboard_path: str,
    cards: list[dict[str, Any]],
    channels: list[dict[str, Any]],
    collection_slug: str | None = None,
    entity_id: str | None = None,
) -> Path:
    path = state_dir / "pulses" / f"{slug}.yaml"
    write_yaml(
        path,
        {
            "entity_id": entity_id,
            "name": name,
            "collection_slug": collection_slug,
            "dashboard_path": dashboard_path,
            "skip_if_empty": False,
            "disable_links": False,
            "archived": False,
            "parameters": [],
            "cards": cards,
            "channels": channels,
        },
    )
    return path


def _tab(t: dict[str, Any]) -> dict[str, Any]:
    return {"entity_id": None, "name": t["name"], "position": t["position"]}


def _dashcard(dc: dict[str, Any]) -> dict[str, Any]:
    return _DASHCARD_DEFAULTS | dc
