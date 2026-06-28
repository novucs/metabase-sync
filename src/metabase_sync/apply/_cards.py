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
                _assert_query_persisted(relpath, desired["dataset_query"], created)
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
                updated = ctx.client.put(f"/api/card/{remote_card['id']}", desired)
                _assert_query_persisted(relpath, desired["dataset_query"], updated)
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
                "template-tags": _normalize_template_tags(
                    fm.get("template_tags") or {}
                ),
            },
        }
    dq = dict(gui_query or {})
    dq["database"] = db_id
    return dq


def _normalize_template_tags(tags: dict[str, Any]) -> dict[str, Any]:
    """Rewrite `dimension` (field-filter) template-tag references to classic
    MBQL form before wrapping the tags in a classic `type: native` query.

    Metabase ≥ v0.61 returns native template-tag dimensions as MBQL5 field
    clauses (`["field", {opts…}, <id>]`, options map first). Sent back inside a
    classic `type: native` query they fail server-side normalisation; the
    classic form `["field", <id>, <opts|null>]` round-trips on both forms.
    """
    out: dict[str, Any] = {}
    for name, tag in tags.items():
        if isinstance(tag, dict) and tag.get("type") == "dimension":
            tag = {**tag, "dimension": _classic_field_ref(tag.get("dimension"))}
        out[name] = tag
    return out


def _classic_field_ref(ref: Any) -> Any:
    """`["field", {opts}, id]` (MBQL5) → `["field", id, opts|null]` (classic).

    Already-classic refs (id in position 1) and any non-`:field` clause are
    returned untouched. `lib/*` bookkeeping and the MBQL5-only `effective-type`
    are dropped from the options; an empty options map becomes `null`.
    """
    if not (isinstance(ref, list) and len(ref) == 3 and ref[0] == "field"):
        return ref
    opts, ident = ref[1], ref[2]
    if not isinstance(opts, dict):
        return ref  # already classic: ["field", <id>, <opts|null>]
    cleaned = {
        k: v
        for k, v in opts.items()
        if not k.startswith("lib/") and k != "effective-type"
    }
    return ["field", ident, cleaned or None]


def _has_query(dq: dict[str, Any] | None) -> bool:
    """Whether a dataset_query actually carries a query (vs the empty `{}` some
    Metabase versions store after a failed normalisation)."""
    if not dq:
        return False
    if (dq.get("native") or {}).get("query"):
        return True
    if isinstance(dq.get("stages"), list) and dq["stages"]:
        return True
    return bool(dq.get("query"))


def _assert_query_persisted(
    relpath: str, sent: dict[str, Any], returned: dict[str, Any]
) -> None:
    """Fail loudly if we sent a non-empty query but the server stored an empty
    one. Some versions answer such a write with HTTP 2xx, so without this check
    the apply would report success while having wiped the card."""
    if not _has_query(sent):
        return
    if not _has_query(returned.get("dataset_query")):
        raise RuntimeError(
            f"{relpath}: Metabase accepted the write (HTTP 2xx) but stored an "
            f"empty dataset_query — the card was not updated correctly. Aborting "
            f"before further cards are touched."
        )


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

    remote_dq = _remote_dataset_query(remote)
    if remote_dq is None:
        return diffs
    if remote_dq.get("__unparseable__"):
        diffs.append(("dataset_query", "<unparseable>", "<rebuilt>"))
        return diffs
    diffs.extend(_dataset_query_diffs(remote_dq, desired["dataset_query"]))
    return diffs


def _remote_dataset_query(remote: dict[str, Any]) -> dict[str, Any] | None:
    """The remote card's query in whatever form Metabase returns.

    For native cards the live `dataset_query` is authoritative and is preferred
    whenever it carries SQL: some versions also expose a `legacy_query`
    projection that can lag behind it (stale SQL), which would otherwise read as
    a perpetual false-positive diff and re-PUT the card on every apply.

    For structured/GUI cards `dataset_query` carries no native SQL, so fall back
    to the classic `legacy_query` JSON when present, else `dataset_query`.
    """
    dq = remote.get("dataset_query")
    dq = dq if isinstance(dq, dict) else None
    if dq is not None and _native_sql(dq) is not None:
        return dq
    legacy = remote.get("legacy_query")
    if legacy:
        try:
            return json.loads(legacy)
        except json.JSONDecodeError:
            return {"__unparseable__": True}
    return dq


def _dataset_query_diffs(
    remote_dq: dict[str, Any], desired_dq: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """Compare desired vs remote query, robust to the classic-vs-MBQL5 form
    split. Native SQL is compared by extracting the SQL string from either
    form; GUI/structured queries are compared structurally after stripping the
    volatile `lib/uuid` keys.
    """
    desired_sql = _native_sql(desired_dq)
    remote_sql = _native_sql(remote_dq)

    if desired_sql is not None or remote_sql is not None:
        # At least one side is a native (SQL) query.
        if (desired_sql is None) != (remote_sql is None):
            return [
                (
                    "query_type",
                    "native" if remote_sql is not None else "query",
                    "native" if desired_sql is not None else "query",
                )
            ]
        if desired_sql != remote_sql:
            return [
                ("SQL", _sql_change_summary(remote_sql or "", desired_sql or ""), None)
            ]
        # SQL identical. Compare template tags only when both sides expose them
        # in classic form (MBQL5 reshapes tags; comparing across forms would
        # produce spurious diffs and break idempotency).
        d_tags = _classic_native_tags(desired_dq)
        r_tags = _classic_native_tags(remote_dq)
        if d_tags is not None and r_tags is not None and d_tags != r_tags:
            return [("template_tags", sorted(r_tags), sorted(d_tags))]
        return []

    # Both GUI/structured. Same-form compare after stripping volatile keys.
    if normalize_dataset_query(remote_dq) != normalize_dataset_query(desired_dq):
        return [("MBQL query structure", "(changed)", None)]
    return []


def _native_sql(dq: dict[str, Any]) -> str | None:
    """Extract the SQL string from a native query in classic or MBQL5 form.
    Returns None if the query isn't native."""
    if dq.get("type") == "native":
        return (dq.get("native") or {}).get("query") or ""
    stages = dq.get("stages")
    if isinstance(stages, list) and stages:
        stage = stages[0]
        if isinstance(stage, dict) and stage.get("lib/type") == "mbql.stage/native":
            sql = stage.get("native")
            return sql if isinstance(sql, str) else ""
    return None


def _classic_native_tags(dq: dict[str, Any]) -> set[str] | None:
    """Template-tag names for a classic-form native query, else None (we don't
    reliably parse MBQL5-form tags)."""
    if dq.get("type") == "native":
        return set((dq.get("native") or {}).get("template-tags") or {})
    return None


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
