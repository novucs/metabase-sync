"""Apply cards — native + GUI.

Two interesting bits:

* `dataset_query` is rebuilt from disk: SQL body for native cards, the stored
  full dataset_query dict for GUI cards (we accept either classic or MBQL5).
* `result_metadata` is reset to `[]` only when the dataset_query itself
  changed. For cosmetic edits (display, viz settings) we pass the server's
  existing metadata through so Metabase doesn't re-run the underlying query.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from metabase_sync.plan import Change
from metabase_sync.serialize.cards import read_card_file
from metabase_sync.serialize.yamlio import write_frontmatter_sql, write_yaml

from ._shared import (
    ApplyContext,
    normalize,
    normalize_dataset_query,
    summarize_diffs,
)

# Diff field labels that indicate the dataset_query itself changed, so we
# must reset result_metadata. Anything else is metadata-only.
_DATASET_QUERY_DIFF_FIELDS = frozenset(
    {"SQL", "query_type", "MBQL query structure", "template_tags"}
)


def apply_cards(ctx: ApplyContext) -> None:
    for path, fm, body, gui_query in _iter_card_files(ctx.state_dir):
        relpath = str(path.relative_to(ctx.state_dir))
        eid = fm.get("entity_id")
        remote_card = ctx.remote.cards_by_entity.get(eid) if eid else None
        collection_id = _resolve_collection_for_card(
            path, ctx.collection_id_by_disk_path
        )
        db_name = fm.get("database")
        db_id = ctx.db_id_by_name.get(db_name) if db_name else None
        dataset_query = _build_dataset_query(fm, body, gui_query, db_id)

        desired: dict[str, Any] = {
            "name": fm["name"],
            "description": fm.get("description"),
            "type": fm.get("type", "question"),
            "display": fm.get("display", "table"),
            "visualization_settings": fm.get("visualization_settings", {}),
            "parameters": fm.get("parameters", []),
            "collection_id": collection_id,
            "database_id": db_id,
            "dataset_query": dataset_query,
            "enable_embedding": fm.get("enable_embedding", False),
            "embedding_params": fm.get("embedding_params"),
            "cache_ttl": fm.get("cache_ttl"),
            "archived": fm.get("archived", False),
        }

        is_model = fm.get("type") == "model"

        if remote_card is None:
            # New card: ship stored result_metadata for models so curated
            # column descriptions land on first apply; let Metabase recompute
            # for ordinary questions.
            desired["result_metadata"] = (
                list(fm.get("result_metadata") or []) if is_model else []
            )
            ctx.plan.add(
                Change(
                    resource="cards",
                    action="create",
                    relpath=relpath,
                    name=fm["name"],
                    summary=_create_summary(fm, body),
                    details={"display": desired["display"], "database": db_name},
                )
            )
            if ctx.mode == "apply":
                created = ctx.client.post("/api/card", desired)
                ctx.card_id_by_disk_path[path.resolve()] = int(created["id"])
                fm["entity_id"] = created.get("entity_id")
                _rewrite_card_file(path, fm, body, gui_query)
            continue

        ctx.card_id_by_disk_path[path.resolve()] = int(remote_card["id"])
        diffs = _diff_card(desired, remote_card)
        if diffs:
            dq_changed = any(d[0] in _DATASET_QUERY_DIFF_FIELDS for d in diffs)
            if is_model:
                # Never wipe a model's curated result_metadata. If the user
                # has stored an updated copy on disk, prefer it; otherwise
                # pass the remote's existing one through.
                desired["result_metadata"] = list(
                    fm.get("result_metadata")
                    or remote_card.get("result_metadata")
                    or []
                )
            else:
                desired["result_metadata"] = (
                    [] if dq_changed else remote_card.get("result_metadata") or []
                )
            ctx.plan.add(
                Change(
                    resource="cards",
                    action="update",
                    relpath=relpath,
                    name=fm["name"],
                    summary=summarize_diffs(diffs),
                    details={"changes": diffs},
                )
            )
            if ctx.mode == "apply":
                ctx.client.put(f"/api/card/{remote_card['id']}", desired)
        else:
            ctx.plan.add(
                Change(
                    resource="cards", action="skip", relpath=relpath, name=fm["name"]
                )
            )


# --- iteration + path resolution -----------------------------------------------


def _iter_card_files(
    state_dir: Path,
) -> Iterator[tuple[Path, dict, str | None, dict | None]]:
    root = state_dir / "collections"
    if not root.exists():
        return
    for path in sorted(root.rglob("cards/*.sql")):
        fm, body, gui = read_card_file(path)
        yield path, fm, body, gui
    for path in sorted(root.rglob("cards/*.yaml")):
        fm, body, gui = read_card_file(path)
        yield path, fm, body, gui


def _resolve_collection_for_card(
    card_path: Path, collection_id_by_disk_path: dict[Path, int]
) -> int | None:
    cards_dir = card_path.parent
    parent = cards_dir.parent
    # Dashboard-internal card path: <collection>/dashboards/<dash>/cards/<card>.sql
    if parent.parent.name == "dashboards":
        return collection_id_by_disk_path.get(parent.parent.parent.resolve())
    return collection_id_by_disk_path.get(parent.resolve())


# --- dataset_query build + write-back ------------------------------------------


def _build_dataset_query(
    fm: dict[str, Any],
    body: str | None,
    gui_query: dict[str, Any] | None,
    db_id: int | None,
) -> dict[str, Any]:
    if body is not None:
        return {
            "database": db_id,
            "type": "native",
            "native": {
                "query": body,
                "template-tags": fm.get("template_tags") or {},
            },
        }
    dq = dict(gui_query or {})
    dq["database"] = db_id
    return dq


def _rewrite_card_file(
    path: Path, fm: dict[str, Any], body: str | None, gui_query: dict[str, Any] | None
) -> None:
    if body is not None:
        write_frontmatter_sql(path, fm, body)
    else:
        write_yaml(path, fm | {"query": gui_query or {}})


# --- diff ---------------------------------------------------------------------


def _diff_card(
    desired: dict[str, Any], remote: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    diffs: list[tuple[str, Any, Any]] = []
    for k in ("name", "description", "display", "collection_id", "archived"):
        if desired.get(k) != remote.get(k):
            diffs.append((k, remote.get(k), desired.get(k)))
    remote_legacy = remote.get("legacy_query")
    if remote_legacy:
        try:
            remote_dq = json.loads(remote_legacy)
        except json.JSONDecodeError:
            diffs.append(("dataset_query", "<unparseable>", "<rebuilt>"))
            return diffs
        if normalize_dataset_query(desired["dataset_query"]) != normalize_dataset_query(
            remote_dq
        ):
            diffs.extend(_dataset_query_diffs(remote_dq, desired["dataset_query"]))
    return diffs


def _dataset_query_diffs(
    remote_dq: dict[str, Any], desired_dq: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """Break a dataset_query change into specific sub-diffs so the plan report
    is useful (e.g. 'SQL: 76 → 78 lines, +2 −0' rather than '<old> → <new>')."""
    diffs: list[tuple[str, Any, Any]] = []
    if remote_dq.get("type") != desired_dq.get("type"):
        diffs.append(("query_type", remote_dq.get("type"), desired_dq.get("type")))
        return diffs

    if desired_dq.get("type") == "native":
        remote_native = remote_dq.get("native") or {}
        desired_native = desired_dq.get("native") or {}
        remote_sql = remote_native.get("query") or ""
        desired_sql = desired_native.get("query") or ""
        if remote_sql != desired_sql:
            diffs.append(("SQL", _sql_change_summary(remote_sql, desired_sql), None))
        if (remote_native.get("template-tags") or {}) != (
            desired_native.get("template-tags") or {}
        ):
            r = list((remote_native.get("template-tags") or {}).keys())
            d = list((desired_native.get("template-tags") or {}).keys())
            diffs.append(("template_tags", r, d))
    else:
        remote_q = remote_dq.get("query") or {}
        desired_q = desired_dq.get("query") or {}
        if normalize(remote_q) != normalize(desired_q):
            diffs.append(("MBQL query structure", "(changed)", None))
    return diffs


def _sql_change_summary(before: str, after: str) -> str:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    added = removed = 0
    for line in difflib.ndiff(before_lines, after_lines):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return f"{len(before_lines)} → {len(after_lines)} lines, +{added} −{removed}"


def _create_summary(fm: dict[str, Any], body: str | None) -> str:
    base = fm.get("name", "")
    if body is not None:
        preview = body.strip().split("\n", 1)[0][:60]
        return f"{base} — {preview}"
    return f"{base} (GUI)"
