"""Apply snippets — POST new, PUT changed, bind to owning collection via
on-disk path."""

from __future__ import annotations

from metabase_sync.plan import Change
from metabase_sync.serialize.snippets import (
    read_snippets,
    resolve_snippet_collection_dir,
)
from metabase_sync.serialize.yamlio import write_frontmatter_sql

from ._shared import ApplyContext, diff_fields, find_by_name, summarize_diffs


def apply_snippets(ctx: ApplyContext) -> None:
    for path, fm, body in read_snippets(ctx.state_dir):
        relpath = str(path.relative_to(ctx.state_dir))
        eid = fm.get("entity_id")
        remote_snippet = (
            ctx.remote.snippets_by_entity.get(eid)
            if eid
            else find_by_name(ctx.remote.snippets_by_id, fm["name"])
        )

        collection_dir = resolve_snippet_collection_dir(ctx.state_dir, path)
        collection_id: int | None = (
            ctx.collection_id_by_disk_path.get(collection_dir.resolve())
            if collection_dir is not None
            else None
        )

        desired = {
            "name": fm["name"],
            "description": fm.get("description"),
            "content": body,
            "collection_id": collection_id,
            "archived": fm.get("archived", False),
        }
        if remote_snippet is None:
            ctx.plan.add(
                Change(
                    resource="snippets",
                    action="create",
                    relpath=relpath,
                    name=fm["name"],
                    summary=fm["name"],
                )
            )
            if ctx.mode == "apply":
                created = ctx.client.post("/api/native-query-snippet", desired)
                fm["entity_id"] = created.get("entity_id")
                write_frontmatter_sql(path, fm, body)
            continue

        diffs = diff_fields(
            desired,
            remote_snippet,
            ("name", "description", "content", "collection_id", "archived"),
        )
        if diffs:
            ctx.plan.add(
                Change(
                    resource="snippets",
                    action="update",
                    relpath=relpath,
                    name=fm["name"],
                    summary=summarize_diffs(diffs),
                    details={"changes": diffs},
                )
            )
            if ctx.mode == "apply":
                ctx.client.put(
                    f"/api/native-query-snippet/{remote_snippet['id']}", desired
                )
        else:
            ctx.plan.add(
                Change(
                    resource="snippets", action="skip", relpath=relpath, name=fm["name"]
                )
            )
