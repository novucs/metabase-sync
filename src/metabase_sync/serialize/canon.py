"""Canonical key ordering for everything written to disk.

Exports and apply write-backs must emit identical YAML for identical content:
top-level keys follow a per-resource template (unknown keys keep their original
order after it), nested mappings sort alphabetically (the server's own map
ordering is not stable across versions), and list order is always preserved.
"""

from __future__ import annotations

from typing import Any

CARD = (
    "entity_id",
    "name",
    "description",
    "type",
    "display",
    "database",
    "parameters",
    "visualization_settings",
    "enable_embedding",
    "embedding_params",
    "cache_ttl",
    "archived",
    "template_tags",
    "result_metadata",
    "dataset_query",
)
SNIPPET = ("entity_id", "name", "description", "template_tags", "archived")
COLLECTION = ("entity_id", "name", "description", "authority_level", "archived")
DASHBOARD = (
    "entity_id",
    "name",
    "description",
    "archived",
    "auto_apply_filters",
    "cache_ttl",
    "enable_embedding",
    "embedding_params",
    "position",
    "width",
    "parameters",
    "tabs",
    "dashcards",
)
TAB = ("entity_id", "name", "position")
DASHCARD = (
    "entity_id",
    "card_path",
    "tab_position",
    "row",
    "col",
    "size_x",
    "size_y",
    "parameter_mappings",
    "visualization_settings",
    "series",
    "action_id",
    "inline_parameters",
)
PULSE = (
    "entity_id",
    "name",
    "collection_slug",
    "dashboard_path",
    "skip_if_empty",
    "disable_links",
    "archived",
    "parameters",
    "cards",
    "channels",
)
PULSE_CARD = (
    "card_path",
    "name",
    "display",
    "include_csv",
    "include_xls",
    "format_rows",
    "pivot_results",
    "parameter_mappings",
)
CHANNEL = (
    "entity_id",
    "channel_type",
    "schedule_type",
    "schedule_hour",
    "schedule_day",
    "schedule_frame",
    "enabled",
    "channel_id",
    "recipients",
)

DASHBOARD_CHILDREN = {"tabs": TAB, "dashcards": DASHCARD}
PULSE_CHILDREN = {"cards": PULSE_CARD, "channels": CHANNEL}


# Server-regenerated bookkeeping inside query structures (dataset_query,
# template_tags). Never persisted and never diffed; scoped to queries because
# these words can be legitimate keys in user content elsewhere.
QUERY_VOLATILE_KEYS = frozenset({"info", "lib/uuid", "lib.convert/converted?"})


def strip_query_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: strip_query_volatile(v)
            for k, v in value.items()
            if k not in QUERY_VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [strip_query_volatile(item) for item in value]
    return value


def canonical(
    doc: dict[str, Any],
    template: tuple[str, ...],
    children: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    children = children or {}
    keys = [k for k in template if k in doc] + [k for k in doc if k not in template]
    out: dict[str, Any] = {}
    for key in keys:
        value = doc[key]
        child = children.get(key)
        if child is not None and isinstance(value, list):
            value = [
                canonical(item, child) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            value = _sorted_nested(value)
        out[key] = value
    return out


def _sorted_nested(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sorted_nested(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, list):
        return [_sorted_nested(item) for item in value]
    return value
