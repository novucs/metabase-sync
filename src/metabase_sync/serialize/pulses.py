"""Pulse ↔ <slug>.yaml.

Pulses store recipients by email (the server returns user ids; we rebind on
apply via `/api/user`). Card and dashboard references use `card_path`/`dashboard_path`
— file paths relative to the pulse YAML — so newly authored items can be
referenced before their entity_ids exist.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from metabase_sync.models import Pulse, PulseChannel
from metabase_sync.serialize.canon import PULSE, PULSE_CHILDREN, canonical
from metabase_sync.serialize.cards import CardPaths
from metabase_sync.serialize.dashboards import DashboardPaths
from metabase_sync.serialize.paths import CollectionPaths, disambiguate, slugify
from metabase_sync.serialize.yamlio import load_yaml, write_yaml


def write_pulses(
    state_dir: Path,
    pulses: list[Pulse],
    collection_paths: CollectionPaths,
    card_paths: CardPaths,
    dashboard_paths: DashboardPaths,
) -> None:
    out = state_dir / "pulses"
    out.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    seen_files: set[str] = set()
    for p in pulses:
        slug = disambiguate(slugify(p.name), p.entity_id, taken)
        taken.add(slug)
        filename = f"{slug}.yaml"
        pulse_file = out / filename
        doc = _pulse_to_doc(
            p, pulse_file, collection_paths, card_paths, dashboard_paths
        )
        seen_files.add(filename)
        write_yaml(pulse_file, canonical(doc, PULSE, PULSE_CHILDREN))
    for existing in out.glob("*.yaml"):
        if existing.name not in seen_files:
            existing.unlink()


def read_pulses(state_dir: Path) -> list[tuple[Path, dict]]:
    out = state_dir / "pulses"
    if not out.exists():
        return []
    return [(p, load_yaml(p)) for p in sorted(out.glob("*.yaml"))]


def _pulse_to_doc(
    p: Pulse,
    pulse_file: Path,
    collection_paths: CollectionPaths,
    card_paths: CardPaths,
    dashboard_paths: DashboardPaths,
) -> dict[str, Any]:
    collection_slug = (
        collection_paths.relpath_from_collections(p.collection_id)
        if p.collection_id is not None and collection_paths.known(p.collection_id)
        else None
    )
    pulse_dir = pulse_file.parent
    dashboard_dir = (
        dashboard_paths.directory_for(p.dashboard_id) if p.dashboard_id else None
    )
    dashboard_path = (
        os.path.relpath(dashboard_dir / "dashboard.yaml", pulse_dir)
        if dashboard_dir is not None
        else None
    )

    return {
        "entity_id": p.entity_id,
        "name": p.name,
        "collection_slug": collection_slug,
        "dashboard_path": dashboard_path,
        "skip_if_empty": p.skip_if_empty,
        "disable_links": p.disable_links,
        "archived": p.archived,
        "parameters": p.parameters,
        "cards": [
            {
                "card_path": _relative_card_path(c.id, pulse_dir, card_paths),
                "name": c.name,
                "display": c.display,
                "include_csv": c.include_csv,
                "include_xls": c.include_xls,
                "format_rows": c.format_rows,
                "pivot_results": c.pivot_results,
                "parameter_mappings": c.parameter_mappings,
            }
            for c in p.cards
        ],
        "channels": [_channel_to_doc(ch) for ch in p.channels],
    }


def _relative_card_path(
    card_id: int, from_dir: Path, card_paths: CardPaths
) -> str | None:
    target = card_paths.path_for(card_id)
    if target is None:
        return None
    return os.path.relpath(target, from_dir)


def _channel_to_doc(ch: PulseChannel) -> dict[str, Any]:
    return {
        "entity_id": ch.entity_id,
        "channel_type": ch.channel_type,
        "schedule_type": ch.schedule_type,
        "schedule_hour": ch.schedule_hour,
        "schedule_day": ch.schedule_day,
        "schedule_frame": ch.schedule_frame,
        "enabled": ch.enabled,
        "channel_id": ch.channel_id,
        "recipients": [{"email": r.email} for r in ch.recipients if r.email],
    }
