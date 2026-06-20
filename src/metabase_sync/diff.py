"""Build an index of the remote instance.

Apply uses this to resolve entity_id → id for every resource type. Databases
have no entity_id so we index them by name.
"""

from __future__ import annotations

from typing import Any

from metabase_sync.client import MetabaseClient


class RemoteIndex:
    def __init__(self) -> None:
        self.collections_by_entity: dict[str, dict[str, Any]] = {}
        self.collections_by_id: dict[int, dict[str, Any]] = {}
        self.cards_by_entity: dict[str, dict[str, Any]] = {}
        self.cards_by_id: dict[int, dict[str, Any]] = {}
        self.dashboards_by_entity: dict[str, dict[str, Any]] = {}
        self.dashboards_by_id: dict[int, dict[str, Any]] = {}
        self.snippets_by_entity: dict[str, dict[str, Any]] = {}
        self.snippets_by_id: dict[int, dict[str, Any]] = {}
        self.pulses_by_entity: dict[str, dict[str, Any]] = {}
        self.pulses_by_id: dict[int, dict[str, Any]] = {}
        self.databases_by_name: dict[str, dict[str, Any]] = {}
        self.users_by_email: dict[str, dict[str, Any]] = {}

    def summary(self) -> dict[str, int]:
        return {
            "collections": len(self.collections_by_id),
            "cards": len(self.cards_by_id),
            "dashboards": len(self.dashboards_by_id),
            "snippets": len(self.snippets_by_id),
            "pulses": len(self.pulses_by_id),
            "databases": len(self.databases_by_name),
            "users": len(self.users_by_email),
        }

    def updated_at_snapshot(self) -> dict[str, str]:
        """Map of "<resource>:<entity_id>" → updated_at for every remote item
        we index by entity_id. Used by the optimistic-concurrency check: if
        the same key has a different value between plan and apply, someone
        edited via the UI in between.

        Key is a joined string (not a tuple) so the snapshot round-trips
        through JSON when written into the audit log.
        """
        snap: dict[str, str] = {}
        for resource, by_entity in (
            ("collections", self.collections_by_entity),
            ("cards", self.cards_by_entity),
            ("dashboards", self.dashboards_by_entity),
            ("snippets", self.snippets_by_entity),
            ("pulses", self.pulses_by_entity),
        ):
            for entity_id, item in by_entity.items():
                updated_at = item.get("updated_at")
                if updated_at is not None:
                    snap[f"{resource}:{entity_id}"] = updated_at
        return snap


def build_remote_index(client: MetabaseClient) -> RemoteIndex:
    idx = RemoteIndex()
    for c in client.list_collections():
        if not isinstance(c.get("id"), int):
            continue
        idx.collections_by_id[c["id"]] = c
        if c.get("entity_id"):
            idx.collections_by_entity[c["entity_id"]] = c
    for c in client.list_cards():
        if c.get("archived"):
            continue
        idx.cards_by_id[c["id"]] = c
        if c.get("entity_id"):
            idx.cards_by_entity[c["entity_id"]] = c
    # Dashboards: GET each detail. The list endpoint omits `dashcards` and `tabs`,
    # so the contents diff would always see them as empty otherwise.
    for d in client.list_dashboards():
        if d.get("archived"):
            continue
        detail = client.get_dashboard(d["id"])
        idx.dashboards_by_id[detail["id"]] = detail
        if detail.get("entity_id"):
            idx.dashboards_by_entity[detail["entity_id"]] = detail
    for s in client.list_snippets():
        if s.get("archived"):
            continue
        idx.snippets_by_id[s["id"]] = s
        if s.get("entity_id"):
            idx.snippets_by_entity[s["entity_id"]] = s
    for p in client.list_pulses():
        if p.get("archived"):
            continue
        idx.pulses_by_id[p["id"]] = p
        if p.get("entity_id"):
            idx.pulses_by_entity[p["entity_id"]] = p
    for db in client.list_databases():
        idx.databases_by_name[db["name"]] = db
    for u in client.list_users():
        if u.get("email"):
            idx.users_by_email[u["email"]] = u
    return idx
