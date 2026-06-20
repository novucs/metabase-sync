"""Pull the live Metabase state into the on-disk tree.

Fetch order: collections → databases → snippets → cards (full detail per id) →
dashboards (full detail per id) → pulses. Cards have to be written before
dashboards so the dashboard YAML can emit `card_path` relative refs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from metabase_sync.client import MetabaseClient
from metabase_sync.models import (
    Card,
    Collection,
    Dashboard,
    Database,
    Pulse,
    Snippet,
)
from metabase_sync.serialize.cards import CardPaths, write_card
from metabase_sync.serialize.collections import (
    ROOT_SENTINEL,
    build_paths,
    is_personal,
    write_collections,
)
from metabase_sync.serialize.dashboards import DashboardPaths, write_dashboard
from metabase_sync.serialize.databases import write_databases
from metabase_sync.serialize.pulses import write_pulses
from metabase_sync.serialize.snippets import write_snippets
from metabase_sync.serialize.version import write_stamp

log = logging.getLogger(__name__)


def _progress(items: list, label: str):
    """Yield items with a Rich progress bar around the loop. Falls back to a
    plain iterator if rich is unavailable or stderr isn't a TTY."""
    try:
        import sys as _sys

        from rich.progress import (
            BarColumn,
            MofNCompleteColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
        )

        if not _sys.stderr.isatty():
            return iter(items)

        progress = Progress(
            TextColumn(f"  {label}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            transient=True,
        )

        def gen():
            with progress:
                task = progress.add_task("", total=len(items))
                for item in items:
                    yield item
                    progress.advance(task)

        return gen()
    except ImportError:
        return iter(items)


def run_export(client: MetabaseClient, state_dir: Path) -> None:
    log.info(f"export → {state_dir}")
    state_dir.mkdir(parents=True, exist_ok=True)
    _wipe(state_dir)
    write_stamp(state_dir)

    # 1. Collections
    raw_collections = client.list_collections()
    collections = [Collection.model_validate(c) for c in raw_collections]
    collection_paths = build_paths(state_dir, collections)
    write_collections(collections, collection_paths)
    log.info(
        f"  collections: {len(collection_paths.all_ids())} (skipped personal + root)"
    )

    # 2. Databases
    databases = [Database.model_validate(d) for d in client.list_databases()]
    write_databases(state_dir, databases)
    db_name_by_id = {d.id: d.name for d in databases}
    log.info(f"  databases: {len(databases)}")

    # 3. Snippets
    snippets = [Snippet.model_validate(s) for s in client.list_snippets()]
    write_snippets(state_dir, snippets, collection_paths)
    log.info(f"  snippets: {len(snippets)}")

    # 4. Cards (detail per id — list omits the full dataset_query)
    card_summaries = [c for c in client.list_cards() if not c.get("archived")]
    cards: list[Card] = []
    for summary in _progress(card_summaries, "cards"):
        detail = client.get_card(summary["id"])
        cards.append(Card.model_validate(detail))
    log.info(f"  cards: {len(cards)}")
    card_paths = CardPaths()

    # 5. Dashboards (need detail per id for dashcards)
    dashboard_summaries = [d for d in client.list_dashboards() if not d.get("archived")]
    dashboards: list[Dashboard] = []
    for summary in _progress(dashboard_summaries, "dashboards"):
        detail = client.get_dashboard(summary["id"])
        dashboards.append(Dashboard.model_validate(detail))
    dashboard_paths = DashboardPaths()
    for d in dashboards:
        dashboard_paths.assign(d, collection_paths)
    log.info(f"  dashboards: {len(dashboards)}")

    # 5a. Assign + write card files. Must happen before dashboards/pulses so they can
    # emit relative `card_path` refs.
    skipped_personal: list[Card] = []
    skipped_unresolved: list[Card] = []
    for card in cards:
        if is_personal(collections, card.collection_id):
            skipped_personal.append(card)
            continue
        parent_dir = _card_parent_dir(card, collection_paths, dashboard_paths)
        if parent_dir is None:
            skipped_unresolved.append(card)
            continue
        parent_dir.mkdir(parents=True, exist_ok=True)
        path = card_paths.assign(card, parent_dir)
        write_card(path, card, db_name_by_id)
    if skipped_personal:
        log.info(f"  skipped {len(skipped_personal)} cards in personal collections")
    if skipped_unresolved:
        log.warning(
            f"  warning: {len(skipped_unresolved)} cards have no resolvable parent: "
            f"{[c.id for c in skipped_unresolved]}"
        )

    # 6. Dashboards
    for d in dashboards:
        directory = dashboard_paths.directory_for(d.id)
        if directory is None:
            continue
        write_dashboard(directory, d, card_paths)

    # 7. Pulses
    pulse_summaries = [p for p in client.list_pulses() if not p.get("archived")]
    pulses: list[Pulse] = []
    for summary in _progress(pulse_summaries, "pulses"):
        detail = client.get_pulse(summary["id"])
        pulses.append(Pulse.model_validate(detail))
    write_pulses(state_dir, pulses, collection_paths, card_paths, dashboard_paths)
    log.info(f"  pulses: {len(pulses)}")

    # 8. Out-of-scope warning
    out_of_scope = client.count_out_of_scope()
    if any(v > 0 for v in out_of_scope.values()):
        log.warning("")
        log.warning(
            "WARNING — these resources exist but are NOT yet synced by this tool:"
        )
        for name, count in out_of_scope.items():
            if count:
                log.info(f"  - {count} {name}")
        log.warning(
            "  Track via https://github.com/novucs/metabase-sync/issues if you need them."
        )

    log.warning("done.")


def _card_parent_dir(
    card: Card, collection_paths, dashboard_paths: DashboardPaths
) -> Path | None:
    if card.dashboard_id is not None:
        directory = dashboard_paths.directory_for(card.dashboard_id)
        if directory is None:
            return None
        return directory / "cards"
    if card.collection_id is None:
        return collection_paths.directory_for(ROOT_SENTINEL) / "cards"
    if collection_paths.known(card.collection_id):
        return collection_paths.directory_for(card.collection_id) / "cards"
    return None


def _wipe(state_dir: Path) -> None:
    """Remove existing serialized output so renames don't leave orphan files."""
    for sub in ("collections", "snippets", "databases", "pulses"):
        target = state_dir / sub
        if target.exists():
            _rmtree(target)


def _rmtree(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _rmtree(child)
        path.rmdir()
    else:
        path.unlink()
