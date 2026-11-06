"""Archive remote items that have no file in state/ (Terraform-style destroy).

Runs after every other applier, so `ctx.claimed` holds every remote item a
state file matched. Anything else in a managed collection is an orphan and goes
to the Trash, which keeps it restorable (restore in the UI, then `export`).

Order is dependents first: a dashboard PUT fails if one of its cards is already
archived, and pulses point at dashboards. Collections go deepest first.
"""

from __future__ import annotations

from typing import Any

from metabase_sync.errors import PreflightError
from metabase_sync.plan import Change

from ._shared import ApplyContext

ORDER = ("pulses", "dashboards", "cards", "snippets", "collections")
_ENDPOINT = {
    "pulses": "pulse",
    "dashboards": "dashboard",
    "cards": "card",
    "snippets": "native-query-snippet",
    "collections": "collection",
}
_STATE_DIRS = ("collections", "snippets", "pulses")


def apply_archive(ctx: ApplyContext, selected: set[str]) -> None:
    unmanaged = unmanaged_collections(ctx.remote.collections_by_id)
    orphans = [
        (res, item)
        for res in ORDER
        if res in selected
        for item in _orphans(ctx, res, unmanaged)
    ]
    if not orphans:
        return
    if not any(
        p.is_file()
        for d in _STATE_DIRS
        if (ctx.state_dir / d).exists()
        for p in (ctx.state_dir / d).rglob("*")
    ):
        raise PreflightError(
            f"{ctx.state_dir} holds no collections, cards, dashboards, snippets, "
            f"or pulses; refusing to archive {len(orphans)} remote item(s). "
            "Wrong --state-dir? Run `metabase-sync export` first."
        )
    cols = ctx.remote.collections_by_id
    archived_dashboards: set[int] = set()
    for res, item in orphans:
        ctx.plan.add(
            Change(
                resource=res,  # type: ignore[arg-type]
                action="archive",
                relpath=_where(cols, res, item),
                name=str(item.get("name")),
                summary=f"{res[:-1]} {item['id']}",
                details={"id": item["id"]},
            )
        )
        if ctx.mode != "apply":
            continue
        if res == "dashboards":
            archived_dashboards.add(int(item["id"]))
        if res == "cards" and item.get("dashboard_id") in archived_dashboards:
            continue  # dashboard question: trashed with its dashboard
        ctx.client.put(f"/api/{_ENDPOINT[res]}/{item['id']}", {"archived": True})


def _orphans(ctx: ApplyContext, res: str, unmanaged: set[int]) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = getattr(ctx.remote, f"{res}_by_id")
    cols, dash = ctx.remote.collections_by_id, ctx.remote.dashboards_by_id

    def managed(cid: Any) -> bool:
        # Root (None) is managed; ids missing from the collection list (other
        # users' personal collections, snippet folders) are never exported.
        return cid is None or (cid in cols and cid not in unmanaged)

    out = []
    for i, item in by_id.items():
        if (res, i) in ctx.claimed or item.get("archived"):
            continue
        if res == "collections":
            if i in unmanaged:
                continue
        elif not managed(item.get("collection_id")):
            continue
        elif res == "pulses" and item.get("dashboard_id") is not None:
            d = dash.get(item["dashboard_id"])
            if d is None or not managed(d.get("collection_id")):
                continue
        out.append(item)
    if res == "collections":
        out.sort(key=lambda c: -len(_location_ids(c)))
    return out


def unmanaged_collections(by_id: dict[int, dict[str, Any]]) -> set[int]:
    """Personal, Trash, instance-analytics, and sample collections plus every
    descendant: export never writes them, so they are never orphans."""
    roots = {
        i
        for i, c in by_id.items()
        if c.get("personal_owner_id") is not None
        or c.get("is_personal")
        or c.get("type")
        or c.get("is_sample")
    }
    return {i for i, c in by_id.items() if i in roots or roots & set(_location_ids(c))}


def _where(cols: dict[int, dict[str, Any]], res: str, item: dict[str, Any]) -> str:
    """Remote location in Metabase terms, e.g. 'Finance/Q3/Old card'."""
    if res == "snippets":
        return f"Snippets/{item.get('name')}"
    cid = item["id"] if res == "collections" else item.get("collection_id")
    c = cols.get(cid) if cid is not None else None
    path = (
        [cols[i]["name"] for i in _location_ids(c) if i in cols] + [c["name"]]
        if c
        else ["Our analytics"]
    )
    if res != "collections":
        path.append(str(item.get("name")))
    return "/".join(path)


def _location_ids(c: dict[str, Any]) -> list[int]:
    return [int(p) for p in str(c.get("location") or "").split("/") if p.isdigit()]
