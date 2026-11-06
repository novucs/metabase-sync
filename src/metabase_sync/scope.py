"""`--collection`: limit export/plan/apply to one top-level collection subtree.

The scope is sticky: export and apply save the collection's entity_id to
`state/.metabase-sync-scope`, and later runs read it, so an unscoped apply can
never run against a scoped state tree. Names are only used to find the
collection the first time, among top-level collections; after that the
entity_id is used, so renames (in the UI or in `_collection.yaml`) are safe.

Scoping happens at the client: `ScopedClient` lists only the subtree, so every
per-item detail fetch (cards, dashboards, pulses) is limited to it as well.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metabase_sync.client import MetabaseClient
from metabase_sync.errors import ScopeError
from metabase_sync.serialize.yamlio import load_yaml

SCOPE_FILE = ".metabase-sync-scope"


def read_scope(state_dir: Path) -> str | None:
    f = state_dir / SCOPE_FILE
    return (f.read_text().strip() or None) if f.exists() else None


def write_scope(state_dir: Path, entity_id: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / SCOPE_FILE).write_text(entity_id + "\n")


def resolve(collections: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    """The top-level collection with this entity_id, else with this name."""
    roots = [
        c
        for c in collections
        if isinstance(c.get("id"), int)
        and c.get("location") == "/"
        and c.get("personal_owner_id") is None
        and not c.get("type")
    ]
    hits = [c for c in roots if c.get("entity_id") == selector] or [
        c for c in roots if c.get("name") == selector
    ]
    if len(hits) > 1:
        ids = ", ".join(str(c.get("entity_id")) for c in hits)
        raise ScopeError(
            f"{len(hits)} top-level collections are named '{selector}'; "
            f"pass one entity_id instead: {ids}"
        )
    return hits[0] if hits else None


def open_scope(
    client: MetabaseClient, state_dir: Path, flag: str | None, *, export: bool
) -> tuple[Any, dict[str, Any] | None]:
    """(client to use, target collection). Unscoped runs get the client back
    unchanged and target None. Export needs the target to exist; plan/apply
    create it on first apply and require state/ to hold only the scope root."""
    saved = read_scope(state_dir)
    selector = flag or saved
    if selector is None:
        return client, None
    cols = client.list_collections()
    target = resolve(cols, selector)
    if flag and saved and flag != saved and (target or {}).get("entity_id") != saved:
        raise ScopeError(
            f"--collection {flag!r} does not match the scope saved in "
            f"{state_dir / SCOPE_FILE} ({saved}). Use a separate --state-dir, or "
            "delete that file to rescope."
        )
    if export:
        if target is None:
            raise ScopeError(
                f"no top-level collection matches {selector!r}; run apply to create it"
            )
    else:
        _check_layout(state_dir)
        on_disk = saved_entity_id(state_dir)
        if target and on_disk and on_disk != target.get("entity_id"):
            raise ScopeError(
                f"state/ holds collection {on_disk}, but the scope is "
                f"{target.get('entity_id')} ('{target.get('name')}')"
            )
    return ScopedClient(client, target, cols), target


def saved_entity_id(state_dir: Path) -> str | None:
    """entity_id of the single top-level collection dir (written back by apply)."""
    dirs = _top_dirs(state_dir)
    if len(dirs) != 1:
        return None
    eid = load_yaml(dirs[0] / "_collection.yaml").get("entity_id")
    return str(eid) if eid else None


def _top_dirs(state_dir: Path) -> list[Path]:
    root = state_dir / "collections"
    if not root.exists():
        return []
    return sorted(d for d in root.iterdir() if (d / "_collection.yaml").exists())


def _check_layout(state_dir: Path) -> None:
    root = state_dir / "collections"
    extra = sorted(
        d.name
        for d in (root.iterdir() if root.exists() else [])
        if d.is_dir() and not (d / "_collection.yaml").exists()
    )
    dirs = _top_dirs(state_dir)
    if len(dirs) > 1 or extra:
        names = ", ".join([d.name for d in dirs] + extra)
        raise ScopeError(
            f"scoped state must hold exactly one top-level collection under "
            f"{root}; found: {names}"
        )


class ScopedClient:
    """Wraps a client so list calls return only the target subtree. Everything
    else (writes, databases, users, detail GETs) passes through."""

    def __init__(
        self,
        client: MetabaseClient,
        target: dict[str, Any] | None,
        all_collections: list[dict[str, Any]],
    ):
        self._client = client
        self._target = target
        self._all = all_collections
        self._cols: list[dict[str, Any]] | None = None
        self._dashes: list[dict[str, Any]] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def list_collections(self) -> list[dict[str, Any]]:
        if self._cols is None:
            t = self._target
            sub = f"/{t['id']}/" if t else ""
            self._cols = (
                [
                    c
                    for c in self._all
                    if isinstance(c.get("id"), int)
                    and (c["id"] == t["id"] or str(c.get("location")).startswith(sub))
                ]
                if t
                else []
            )
        return self._cols

    def _ids(self) -> set[int]:
        return {c["id"] for c in self.list_collections()}

    def list_cards(self) -> list[dict[str, Any]]:
        ids = self._ids()
        if not ids:
            return []
        return [c for c in self._client.list_cards() if c.get("collection_id") in ids]

    def list_dashboards(self) -> list[dict[str, Any]]:
        if self._dashes is None:
            ids = self._ids()
            self._dashes = (
                [
                    d
                    for d in self._client.list_dashboards()
                    if d.get("collection_id") in ids
                ]
                if ids
                else []
            )
        return self._dashes

    def list_pulses(self) -> list[dict[str, Any]]:
        dash = {d["id"] for d in self.list_dashboards()}
        if not dash:
            return []
        return [p for p in self._client.list_pulses() if p.get("dashboard_id") in dash]

    def list_snippets(self) -> list[dict[str, Any]]:
        return []  # global namespace, never in a collection scope
