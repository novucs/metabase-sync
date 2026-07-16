"""Apply snippets — POST new, PUT changed.

Snippet folders are NOT yet supported: Metabase keeps snippet collections in a
separate `:snippets` namespace, distinct from the card/dashboard collection
tree this tool models. Sending a normal collection id to the snippet endpoint is
rejected with a 400, which would abort the whole apply. So a snippet authored
under `collections/<x>/snippets/` is applied to the root snippets namespace
(collection_id=None) with a one-time warning, rather than crashing. Tracked as a
follow-up (proper `:snippets`-namespace support).
"""

from __future__ import annotations

import logging

from metabase_sync.plan import Change
from metabase_sync.serialize.canon import SNIPPET, canonical
from metabase_sync.serialize.snippets import (
    read_snippets,
    resolve_snippet_collection_dir,
)
from metabase_sync.serialize.yamlio import write_frontmatter_sql

from ._shared import ApplyContext, diff_fields, find_by_name, summarize_diffs

log = logging.getLogger(__name__)


def apply_snippets(ctx: ApplyContext) -> None:
    for path, fm, body in read_snippets(ctx.state_dir):
        relpath = str(path.relative_to(ctx.state_dir))
        eid = fm.get("entity_id")
        remote_snippet = (
            ctx.remote.snippets_by_entity.get(eid)
            if eid
            else find_by_name(ctx.remote.snippets_by_id, fm["name"])
        )

        if resolve_snippet_collection_dir(ctx.state_dir, path) is not None:
            log.warning(
                "snippet %s is under a collection folder, but snippet folders "
                "are not yet supported (Metabase keeps them in a separate "
                "namespace). Applying it to the root snippets namespace.",
                relpath,
            )
        # Snippets can only live in the :snippets namespace, never a normal
        # collection — always None until namespace support lands.
        collection_id: int | None = None

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
                write_frontmatter_sql(path, canonical(fm, SNIPPET), body)
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
