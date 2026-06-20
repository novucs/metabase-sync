"""Dashboard ↔ <dash-slug>/dashboard.yaml.

A dashboard always becomes a directory so its internal cards (cards with
`dashboard_id == this dashboard.id`) live under `<dash-slug>/cards/`.

Dashcards reference their card by `card_path` (relative to the dashboard directory)
so a card can be authored in code and referenced before its entity_id exists.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from metabase_sync.models import Dashboard, Dashcard, Tab
from metabase_sync.serialize.cards import CardPaths
from metabase_sync.serialize.paths import CollectionPaths, disambiguate, slugify
from metabase_sync.serialize.yamlio import load_yaml, write_yaml


class DashboardPaths:
    def __init__(self) -> None:
        self._dir_by_id: dict[int, Path] = {}
        self._slug_by_id: dict[int, str] = {}

    def assign(self, dashboard: Dashboard, collection_paths: CollectionPaths) -> Path:
        # Root-level dashboards (collection_id=None) land under the synthetic
        # _root pseudo-collection — same routing convention as root-level cards.
        from metabase_sync.serialize.collections import ROOT_SENTINEL

        cid = dashboard.collection_id
        if cid is None:
            cid = ROOT_SENTINEL
        elif not collection_paths.known(cid):
            raise ValueError(
                f"dashboard {dashboard.id} ({dashboard.name}) is in collection "
                f"{dashboard.collection_id} which is not in the path map"
            )
        parent = collection_paths.directory_for(cid) / "dashboards"
        taken = {p.name for p in self._dir_by_id.values() if p.parent == parent}
        slug = disambiguate(slugify(dashboard.name), dashboard.entity_id, taken)
        directory = parent / slug
        self._dir_by_id[dashboard.id] = directory
        self._slug_by_id[dashboard.id] = slug
        return directory

    def directory_for(self, dashboard_id: int) -> Path | None:
        return self._dir_by_id.get(dashboard_id)


def write_dashboard(
    directory: Path,
    dashboard: Dashboard,
    card_paths: CardPaths,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "entity_id": dashboard.entity_id,
        "name": dashboard.name,
        "description": dashboard.description,
        "archived": dashboard.archived,
        "auto_apply_filters": dashboard.auto_apply_filters,
        "cache_ttl": dashboard.cache_ttl,
        "enable_embedding": dashboard.enable_embedding,
        "embedding_params": dashboard.embedding_params,
        "position": dashboard.position,
        "width": dashboard.width,
        "parameters": dashboard.parameters,
        "tabs": [_tab_to_doc(t) for t in dashboard.tabs],
        "dashcards": [
            _dashcard_to_doc(dc, dashboard, directory, card_paths)
            for dc in dashboard.dashcards
        ],
    }
    write_yaml(directory / "dashboard.yaml", doc)


def _tab_to_doc(tab: Tab) -> dict[str, Any]:
    return {
        "entity_id": tab.entity_id,
        "name": tab.name,
        "position": tab.position,
    }


def _dashcard_to_doc(
    dc: Dashcard, dashboard: Dashboard, dashboard_dir: Path, card_paths: CardPaths
) -> dict[str, Any]:
    card_path = _relative_card_path(dc.card_id, dashboard_dir, card_paths)
    tab_position = None
    if dc.dashboard_tab_id is not None:
        for tab in dashboard.tabs:
            if tab.id == dc.dashboard_tab_id:
                tab_position = tab.position
                break

    doc: dict[str, Any] = {
        "entity_id": dc.entity_id,
        "card_path": card_path,
        "tab_position": tab_position,
        "row": dc.row,
        "col": dc.col,
        "size_x": dc.size_x,
        "size_y": dc.size_y,
        "parameter_mappings": dc.parameter_mappings,
        "visualization_settings": dc.visualization_settings,
        "series": dc.series,
    }
    if dc.action_id is not None:
        doc["action_id"] = dc.action_id
    if dc.inline_parameters:
        doc["inline_parameters"] = dc.inline_parameters
    return doc


def _relative_card_path(
    card_id: int | None, from_dir: Path, card_paths: CardPaths
) -> str | None:
    if card_id is None:
        return None
    target = card_paths.path_for(card_id)
    if target is None:
        return None
    return os.path.relpath(target, from_dir)


def read_dashboard_files(state_dir: Path) -> list[tuple[Path, dict]]:
    root = state_dir / "collections"
    if not root.exists():
        return []
    return [(p.parent, load_yaml(p)) for p in sorted(root.rglob("dashboard.yaml"))]
