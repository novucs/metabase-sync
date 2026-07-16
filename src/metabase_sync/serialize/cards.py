"""Card ↔ .sql (native) or .yaml (GUI) file.

Storage decisions:

* Native cards: the SQL body is the literal query string, byte-faithful so
  trailing whitespace round-trips. It is read from the live `dataset_query`
  when that carries SQL, falling back to `legacy_query` (see
  `_native_components` for why the precedence matters).
* GUI cards: store the full `dataset_query` dict. Metabase exposes two shapes:
  - On older versions (≲v0.61), `legacy_query` is the stable "classic"
    `{database, type:'query', query:{...}}` form.
  - On newer versions (≳v0.62 / current), `legacy_query` is the MBQL5
    `{lib/type, database, stages:[...]}` form (the same as `dataset_query`)
    with `lib/uuid` values that the server regenerates on every save.
  We accept either shape on disk and round-trip it back as `dataset_query`
  unchanged. The diff-time normaliser strips `lib/uuid` so MBQL5 cards don't
  produce spurious updates.

Paths are determined by:
  * card.collection_id → collections/<path>/cards/<slug>.{sql|yaml}
  * card.dashboard_id  → collections/<path>/dashboards/<dash-slug>/cards/<slug>.{sql|yaml}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from metabase_sync.models import Card
from metabase_sync.serialize.canon import CARD, canonical
from metabase_sync.serialize.paths import disambiguate, slugify
from metabase_sync.serialize.yamlio import (
    load_yaml,
    read_frontmatter_sql,
    write_frontmatter_sql,
    write_yaml,
)


class CardPaths:
    """Tracks slug allocation per parent directory so two cards with the same name
    inside the same collection or dashboard don't collide."""

    def __init__(self) -> None:
        self._taken_by_dir: dict[Path, set[str]] = {}
        self._path_by_card_id: dict[int, Path] = {}
        self._slug_by_card_id: dict[int, str] = {}

    def assign(self, card: Card, parent_dir: Path) -> Path:
        taken = self._taken_by_dir.setdefault(parent_dir, set())
        slug = disambiguate(slugify(card.name), card.entity_id, taken)
        taken.add(slug)
        is_native = _is_native(card)
        filename = f"{slug}.sql" if is_native else f"{slug}.yaml"
        path = parent_dir / filename
        self._path_by_card_id[card.id] = path
        self._slug_by_card_id[card.id] = slug
        return path

    def slug_for(self, card_id: int) -> str | None:
        return self._slug_by_card_id.get(card_id)

    def path_for(self, card_id: int) -> Path | None:
        return self._path_by_card_id.get(card_id)


def write_card(path: Path, card: Card, db_name_by_id: dict[int, str]) -> None:
    db_name = (
        db_name_by_id.get(card.database_id) if card.database_id is not None else None
    )
    # Only persist result_metadata for models — users curate column descriptions
    # and semantic types on them, and a careless apply would wipe that. For
    # ordinary questions we let Metabase recompute.
    extra: dict[str, Any] = {}
    if card.type == "model" and card.result_metadata is not None:
        extra["result_metadata"] = card.result_metadata

    native = _native_components(card)
    if native is not None:
        body, template_tags = native
        frontmatter = _common_frontmatter(card, db_name) | {
            "template_tags": template_tags,
            **extra,
        }
        write_frontmatter_sql(path, canonical(frontmatter, CARD), body)
    else:
        # GUI card. Pick the most informative dataset_query representation: prefer
        # legacy_query (classic form on old Metabase, MBQL5 on new), fall back to
        # dataset_query when legacy is empty or unparseable.
        legacy = _legacy_dict(card)
        dataset_query = legacy if legacy else dict(card.dataset_query)
        doc = _common_frontmatter(card, db_name) | {
            "dataset_query": dataset_query,
            **extra,
        }
        write_yaml(path, canonical(doc, CARD))


def read_card_file(
    path: Path,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Return (frontmatter, native_body, gui_dataset_query).

    Native cards: native_body is the SQL string, gui_dataset_query is None.
    GUI cards: native_body is None, gui_dataset_query is the full dict (either
    classic `{database, type, query}` or MBQL5 `{lib/type, database, stages}`).
    """
    if path.suffix == ".sql":
        fm, body = read_frontmatter_sql(path)
        return fm, body, None
    doc = load_yaml(path) or {}
    dataset_query = doc.pop("dataset_query", None)
    if dataset_query is None:
        # Backwards compat with the pre-0.1 layout where GUI cards stored only
        # the inner `query` sub-dict in classic form.
        legacy_query = doc.pop("query", {})
        dataset_query = {"type": "query", "query": legacy_query}
    return doc, None, dataset_query


def _is_native(card: Card) -> bool:
    """Use the same predicate as `write_card` so the `.sql`/`.yaml` filename
    extension agrees with what `write_card` actually writes. The pair drifted
    once already — see the v0.55 integration breakage in CI history."""
    return _native_components(card) is not None


def _native_components(card: Card) -> tuple[str, dict[str, Any]] | None:
    """Return (sql_body, template_tags) if this card is native, else None.

    Metabase exposes the native query under different keys across versions:

    * Classic form in `legacy_query`: `{type:'native', native:{query, template-tags}}`.
    * Classic form in `dataset_query`: same shape, used by Metabase v0.55 (and
      earlier) and by cards we POST ourselves before Metabase reshapes them.
    * MBQL5 form in `dataset_query.stages`: a `mbql.stage/native` stage with
      the SQL string inline. Seen on Metabase ≥ v0.62 for cards created via
      the UI.

    The live `dataset_query` wins whenever it carries SQL: after an API write
    Metabase updates it while the `legacy_query` projection can still return the
    pre-write SQL. Exporting that stale copy makes an applied change look like it
    never landed, and re-applying the export would revert the card for real. The
    diff path picks the live query for the same reason (see
    `apply._cards._remote_dataset_query`). `legacy_query` stays the fallback for
    versions that leave `dataset_query` empty.
    """
    live = _native_query(card.dataset_query or {})
    legacy = _native_query(_legacy_dict(card))
    if live is not None and live[0]:
        return live
    if legacy is not None and legacy[0]:
        return legacy
    return live or legacy


def _native_query(query: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """(sql_body, template_tags) for a native query in classic or MBQL5 form."""
    if query.get("type") == "native":
        native = query.get("native")
        if isinstance(native, dict):
            return native.get("query") or "", native.get("template-tags") or {}

    stages = query.get("stages")
    if isinstance(stages, list) and stages:
        stage = stages[0]
        if isinstance(stage, dict) and stage.get("lib/type") == "mbql.stage/native":
            sql = stage.get("native")
            if isinstance(sql, str):
                return sql, stage.get("template-tags") or {}

    return None


def _legacy_dict(card: Card) -> dict[str, Any]:
    if not card.legacy_query:
        return {}
    try:
        parsed = json.loads(card.legacy_query)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _common_frontmatter(card: Card, db_name: str | None) -> dict[str, Any]:
    return {
        "entity_id": card.entity_id,
        "name": card.name,
        "description": card.description,
        "type": card.type,
        "display": card.display,
        "database": db_name,
        "parameters": card.parameters,
        "visualization_settings": card.visualization_settings,
        "enable_embedding": card.enable_embedding,
        "embedding_params": card.embedding_params,
        "cache_ttl": card.cache_ttl,
        "archived": card.archived,
    }
