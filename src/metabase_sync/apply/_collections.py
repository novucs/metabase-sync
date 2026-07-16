"""Apply collections — POST new, PUT changed, persist server-allocated entity_ids."""

from __future__ import annotations

from typing import Any

from metabase_sync.diff import RemoteIndex
from metabase_sync.plan import Change
from metabase_sync.serialize.canon import COLLECTION, canonical
from metabase_sync.serialize.collections import read_collections
from metabase_sync.serialize.yamlio import write_yaml

from ._shared import ApplyContext, diff_fields, summarize_diffs


def apply_collections(ctx: ApplyContext) -> None:
    for directory, manifest in read_collections(ctx.state_dir):
        if directory.name == "root" and directory.parent.name == "collections":
            continue
        parent_dir = directory.parent
        parent_id: int | None = (
            ctx.collection_id_by_disk_path.get(parent_dir.resolve())
            if parent_dir.name != "collections"
            else None
        )
        relpath = str(directory.relative_to(ctx.state_dir))

        eid = manifest.get("entity_id")
        remote_collection = (
            ctx.remote.collections_by_entity.get(eid)
            if eid
            else _find_by_name_and_parent(ctx.remote, parent_id, manifest["name"])
        )
        desired = {
            "name": manifest["name"],
            "description": manifest.get("description"),
            "parent_id": parent_id,
            "authority_level": manifest.get("authority_level"),
            "archived": manifest.get("archived", False),
        }

        if remote_collection is None:
            ctx.plan.add(
                Change(
                    resource="collections",
                    action="create",
                    relpath=relpath,
                    name=manifest["name"],
                    summary=manifest["name"],
                    details={"desired": desired},
                )
            )
            if ctx.mode == "apply":
                created = ctx.client.post("/api/collection", desired)
                ctx.collection_id_by_disk_path[directory.resolve()] = int(created["id"])
                manifest["entity_id"] = created.get("entity_id")
                write_yaml(
                    directory / "_collection.yaml", canonical(manifest, COLLECTION)
                )
            continue

        ctx.collection_id_by_disk_path[directory.resolve()] = int(
            remote_collection["id"]
        )
        diffs = diff_fields(
            desired,
            remote_collection,
            ("name", "description", "parent_id", "authority_level", "archived"),
        )
        if diffs:
            ctx.plan.add(
                Change(
                    resource="collections",
                    action="update",
                    relpath=relpath,
                    name=manifest["name"],
                    summary=summarize_diffs(diffs),
                    details={"changes": diffs},
                )
            )
            if ctx.mode == "apply":
                ctx.client.put(f"/api/collection/{remote_collection['id']}", desired)
        else:
            ctx.plan.add(
                Change(
                    resource="collections",
                    action="skip",
                    relpath=relpath,
                    name=manifest["name"],
                )
            )
        if eid != remote_collection.get("entity_id") and ctx.mode == "apply":
            manifest["entity_id"] = remote_collection.get("entity_id")
            write_yaml(directory / "_collection.yaml", canonical(manifest, COLLECTION))


def _find_by_name_and_parent(
    remote: RemoteIndex, parent_id: int | None, name: str
) -> dict[str, Any] | None:
    for c in remote.collections_by_id.values():
        if c.get("parent_id") == parent_id and c.get("name") == name:
            return c
    return None
