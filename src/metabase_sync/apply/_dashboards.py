"""Apply dashboards — single PUT carries metadata + tabs + dashcards together
so a dashboard can never be left half-applied."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metabase_sync.client import MetabaseClient
from metabase_sync.diff import RemoteIndex
from metabase_sync.errors import ReferenceResolutionError
from metabase_sync.plan import Change
from metabase_sync.serialize.dashboards import read_dashboard_files
from metabase_sync.serialize.yamlio import write_yaml

from ._shared import ApplyContext, diff_fields, resolve_card_path, summarize_diffs


def apply_dashboards(ctx: ApplyContext) -> None:
    for directory, doc in read_dashboard_files(ctx.state_dir):
        dashboard_file = directory / "dashboard.yaml"
        relpath = str(dashboard_file.relative_to(ctx.state_dir))
        eid = doc.get("entity_id")
        collection_id = _resolve_collection_for_dashboard(
            directory, ctx.collection_id_by_disk_path
        )
        remote_dashboard = (
            ctx.remote.dashboards_by_entity.get(eid)
            if eid
            else _find_by_collection_and_name(ctx.remote, collection_id, doc["name"])
        )

        desired: dict[str, Any] = {
            "name": doc["name"],
            "description": doc.get("description"),
            "collection_id": collection_id,
            "parameters": doc.get("parameters", []),
            "auto_apply_filters": doc.get("auto_apply_filters", True),
            "cache_ttl": doc.get("cache_ttl"),
            "enable_embedding": doc.get("enable_embedding", False),
            "embedding_params": doc.get("embedding_params"),
            "width": doc.get("width", "fixed"),
            "archived": doc.get("archived", False),
        }

        if remote_dashboard is None:
            ctx.plan.add(
                Change(
                    resource="dashboards",
                    action="create",
                    relpath=relpath,
                    name=doc["name"],
                    summary=f"{doc['name']} ({len(doc.get('dashcards') or [])} dashcards)",
                )
            )
            if ctx.mode == "apply":
                # Two-call create: POST allocates an id; PUT installs the full
                # metadata + tabs + dashcards in one go (the create POST
                # doesn't accept nested dashcards on most Metabase versions).
                created = ctx.client.post(
                    "/api/dashboard",
                    {"name": doc["name"], "collection_id": collection_id},
                )
                dashboard_id = int(created["id"])
                ctx.dashboard_id_by_disk_path[directory.resolve()] = dashboard_id
                doc["entity_id"] = created.get("entity_id")
                write_yaml(dashboard_file, doc)
                _put_full(
                    ctx.client,
                    dashboard_id,
                    desired,
                    doc,
                    directory,
                    ctx.card_id_by_disk_path,
                )
            continue

        ctx.dashboard_id_by_disk_path[directory.resolve()] = int(remote_dashboard["id"])
        diffs = diff_fields(
            desired,
            remote_dashboard,
            ("name", "description", "collection_id", "auto_apply_filters", "archived"),
        )
        contents_diff = _contents_diff(
            doc, remote_dashboard, directory, ctx.card_id_by_disk_path
        )
        if diffs or contents_diff:
            summary_parts = []
            if diffs:
                summary_parts.append(summarize_diffs(diffs))
            if contents_diff:
                summary_parts.append(contents_diff)
            ctx.plan.add(
                Change(
                    resource="dashboards",
                    action="update",
                    relpath=relpath,
                    name=doc["name"],
                    summary="; ".join(summary_parts),
                    details={"changes": diffs, "contents": contents_diff},
                )
            )
            if ctx.mode == "apply":
                _put_full(
                    ctx.client,
                    int(remote_dashboard["id"]),
                    desired,
                    doc,
                    directory,
                    ctx.card_id_by_disk_path,
                )
        else:
            ctx.plan.add(
                Change(
                    resource="dashboards",
                    action="skip",
                    relpath=relpath,
                    name=doc["name"],
                )
            )


def _resolve_collection_for_dashboard(
    dashboard_dir: Path, collection_id_by_disk_path: dict[Path, int]
) -> int | None:
    return collection_id_by_disk_path.get(dashboard_dir.parent.parent.resolve())


def _find_by_collection_and_name(
    remote: RemoteIndex, collection_id: int | None, name: str
) -> dict[str, Any] | None:
    for d in remote.dashboards_by_id.values():
        if d.get("collection_id") == collection_id and d.get("name") == name:
            return d
    return None


def _contents_diff(
    doc: dict[str, Any],
    remote_dashboard: dict[str, Any],
    dashboard_dir: Path,
    card_id_by_disk_path: dict[Path, int],
) -> str:
    """One-line summary if disk dashcards/tabs differ from the remote, else ''.

    Surfaces both tabs and dashcards drift in the same summary so a dashboard
    that changed both doesn't show only the tabs delta.
    """
    parts: list[str] = []

    desired_tabs = [(t.get("name"), t.get("position")) for t in (doc.get("tabs") or [])]
    remote_tabs = [
        (t.get("name"), t.get("position")) for t in (remote_dashboard.get("tabs") or [])
    ]
    if desired_tabs != remote_tabs:
        parts.append(f"tabs: {len(remote_tabs)} → {len(desired_tabs)}")

    desired_dc = [
        (
            resolve_card_path(dc.get("card_path"), dashboard_dir, card_id_by_disk_path),
            dc.get("row"),
            dc.get("col"),
            dc.get("size_x"),
            dc.get("size_y"),
        )
        for dc in (doc.get("dashcards") or [])
    ]
    remote_dc = [
        (
            dc.get("card_id"),
            dc.get("row"),
            dc.get("col"),
            dc.get("size_x"),
            dc.get("size_y"),
        )
        for dc in (remote_dashboard.get("dashcards") or [])
    ]
    if desired_dc != remote_dc:
        parts.append(f"dashcards: {len(remote_dc)} → {len(desired_dc)}")

    return "; ".join(parts)


# --- write side --------------------------------------------------------------


def _put_full(
    client: MetabaseClient,
    dashboard_id: int,
    metadata: dict[str, Any],
    doc: dict[str, Any],
    dashboard_dir: Path,
    card_id_by_disk_path: dict[Path, int],
) -> None:
    """Single PUT carrying metadata + tabs + dashcards.

    Metabase's dashboard PUT accepts all these fields in one request, so we
    fire one call rather than two — eliminates the half-applied window where
    metadata had been updated but dashcards hadn't.
    """
    tabs_payload, pos_to_temp_id = _build_tabs_payload(doc)
    dashcards_payload = _build_dashcards_payload(
        doc, dashboard_dir, card_id_by_disk_path, pos_to_temp_id
    )
    body = {**metadata, "tabs": tabs_payload, "dashcards": dashcards_payload}
    client.put(f"/api/dashboard/{dashboard_id}", body)


def _build_tabs_payload(
    doc: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    tabs_payload: list[dict[str, Any]] = []
    pos_to_temp_id: dict[int, int] = {}
    for i, tab in enumerate(doc.get("tabs", []) or []):
        temp_id = -(i + 1)
        pos_to_temp_id[int(tab["position"])] = temp_id
        tabs_payload.append(
            {"id": temp_id, "name": tab["name"], "position": tab["position"]}
        )
    return tabs_payload, pos_to_temp_id


def _build_dashcards_payload(
    doc: dict[str, Any],
    dashboard_dir: Path,
    card_id_by_disk_path: dict[Path, int],
    pos_to_temp_id: dict[int, int],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, dc in enumerate(doc.get("dashcards", []) or []):
        card_id = resolve_card_path(
            dc.get("card_path"), dashboard_dir, card_id_by_disk_path
        )
        if card_id is None and dc.get("card_path") is not None:
            raise ReferenceResolutionError(
                f"dashboard {dashboard_dir}: dashcard references "
                f"card_path={dc['card_path']} but no such card file exists"
            )
        tab_pos = dc.get("tab_position")
        out.append(
            {
                "id": -(i + 1),
                "card_id": card_id,
                "dashboard_tab_id": pos_to_temp_id.get(tab_pos)
                if tab_pos is not None
                else None,
                "row": dc["row"],
                "col": dc["col"],
                "size_x": dc["size_x"],
                "size_y": dc["size_y"],
                "parameter_mappings": dc.get("parameter_mappings", []),
                "visualization_settings": dc.get("visualization_settings", {}),
                "series": dc.get("series", []),
                "action_id": dc.get("action_id"),
            }
        )
    return out
