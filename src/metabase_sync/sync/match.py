import json
from dataclasses import replace
from typing import Any

from metabase_sync.format import representations
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    CONTAINERS,
    DASHBOARD,
    SNIPPET,
    SUBSCRIPTION,
    Item,
)
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.tree import Entity, LocalTree


def find_imports(tree: LocalTree, remote: Remote) -> tuple[dict[str, str], set[str]]:
    index = remote.index
    pending = [
        e
        for e in tree.entities.values()
        if e.entity_id not in tree.managed and index.find(e.entity_id) is None
    ]
    renames: dict[str, str] = {}
    ambiguous: set[str] = set()
    if not pending:
        return renames, ambiguous
    candidates_by_kind = {
        kind: {
            item.entity_id: remote.item(kind, item)
            for item in index.live(kind)
            if item.entity_id
            and not item.document_id
            and item.entity_id not in tree.entities
            and item.entity_id not in tree.managed
        }
        for kind in {e.kind for e in pending}
    }
    for group in _match_groups(pending, tree):
        claims: dict[str, list[Entity]] = {}
        for entity in group:
            wanted = _identity(entity.kind, entity.data, renames)
            candidates = [
                eid
                for eid, item in candidates_by_kind[entity.kind].items()
                if eid not in renames.values()
                and _identity(entity.kind, item, {}) == wanted
            ]
            if len(candidates) == 1:
                claims.setdefault(candidates[0], []).append(entity)
            elif candidates:
                ambiguous.add(entity.entity_id)
        for eid, claimants in claims.items():
            if len(claimants) > 1:
                ambiguous.update(e.entity_id for e in claimants)
            else:
                renames[claimants[0].entity_id] = eid
    for local_eid, remote_eid in list(renames.items()):
        local = tree.entities[local_eid].data
        theirs = candidates_by_kind.get(DASHBOARD, {}).get(remote_eid)
        if isinstance(local, Dashboard) and isinstance(theirs, Dashboard):
            renames.update(_nested_matches(local, theirs, renames))
    return renames, ambiguous


def renamed(tree: LocalTree, renames: dict[str, str]) -> LocalTree:
    entities = [
        replace(e, data=representations.swapped(e.data, renames))
        for e in tree.entities.values()
    ]
    return replace(tree, entities={e.entity_id: e for e in entities})


def _match_groups(entities: list[Entity], tree: LocalTree) -> list[list[Entity]]:
    def depth(entity: Entity) -> int:
        seen, cursor = 0, tree.get(representations.collection_of(entity.data))
        while cursor is not None and seen < 100:
            seen += 1
            cursor = tree.get(representations.collection_of(cursor.data))
        return seen

    collections: dict[int, list[Entity]] = {}
    for entity in entities:
        if entity.kind == COLLECTION:
            collections.setdefault(depth(entity), []).append(entity)
    groups = [collections[level] for level in sorted(collections)]
    for kind in (SNIPPET, DASHBOARD, CARD, SUBSCRIPTION):
        groups.append([e for e in entities if e.kind == kind])
    return groups


def _identity(kind: str, item: Item, renames: dict[str, str]) -> Any:
    places = tuple(
        renames.get(ref, ref) if isinstance(ref, str) else ref
        for ref in (getattr(item, name) for name in CONTAINERS[kind])
    )
    if isinstance(item, Subscription):
        destinations = sorted(
            json.dumps([c.channel_type, c.details, c.recipients], sort_keys=True)
            for c in item.channels
        )
        return places, destinations
    return item.name, places


def _nested_matches(
    local: Dashboard, remote: Dashboard, renames: dict[str, str]
) -> dict[str, str]:
    found: dict[str, str] = {}
    remote_tabs = {(tab.name, tab.position): tab.entity_id for tab in remote.tabs}
    for tab in local.tabs:
        match = remote_tabs.get((tab.name, tab.position))
        if match:
            found[tab.entity_id] = match
    known = {**renames, **found}
    remote_cards: dict[Any, list[str]] = {}
    for dc in remote.dashcards:
        key = (dc.tab, dc.card_id, dc.row, dc.col)
        remote_cards.setdefault(key, []).append(dc.entity_id)
    for dc in local.dashcards:
        tab = known.get(dc.tab, dc.tab) if dc.tab else None
        card = known.get(dc.card_id, dc.card_id) if dc.card_id else dc.card_id
        key = (tab, card, dc.row, dc.col)
        if len(remote_cards.get(key, [])) == 1:
            found[dc.entity_id] = remote_cards[key][0]
    return found
