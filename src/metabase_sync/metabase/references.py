import json
from collections.abc import Callable
from typing import Any

from metabase_sync.format.representations import CARD, COLLECTION, DASHBOARD, SNIPPET
from metabase_sync.metabase.index import Index

DATABASE = "database"
TABLE = "table"
FIELD = "field"
KEYS = {
    "database": DATABASE,
    "source-table": TABLE,
    "source-card": CARD,
    "card-id": CARD,
    "card_id": CARD,
    "snippet-id": SNIPPET,
    "source-field": FIELD,
    "fk_target_field_id": FIELD,
}
LINKED = {
    "card": CARD,
    "dataset": CARD,
    "metric": CARD,
    "dashboard": DASHBOARD,
    "collection": COLLECTION,
    "table": TABLE,
    "database": DATABASE,
}

type Resolve = Callable[[str, Any], Any]


def portable(value: Any, index: Index) -> Any:
    def resolve(kind: str, ref: Any) -> Any:
        if type(ref) is not int:
            return ref
        if kind == DATABASE:
            return index.lenient(index.database_name, ref, DATABASE)
        if kind == TABLE:
            return index.lenient(index.metadata.table_ref, ref, TABLE)
        if kind == FIELD:
            return index.lenient(index.metadata.field_ref, ref, FIELD)
        return index.lenient(lambda r: index.entity_id(kind, r), ref, kind)

    return walk(value, resolve)


def native(value: Any, index: Index) -> Any:
    def resolve(kind: str, ref: Any) -> Any:
        if kind == DATABASE:
            return index.metadata.database_id(ref) if isinstance(ref, str) else ref
        if kind == TABLE:
            return index.metadata.table_id(ref) if isinstance(ref, list) else ref
        if kind == FIELD:
            return index.metadata.field_id(ref) if isinstance(ref, list) else ref
        return index.id(kind, ref) if isinstance(ref, str) else ref

    return walk(value, resolve)


def walk(value: Any, resolve: Resolve) -> Any:
    if isinstance(value, list):
        if len(value) == 3 and value[0] == "field":
            if isinstance(value[1], dict):
                return ["field", walk(value[1], resolve), resolve(FIELD, value[2])]
            return ["field", resolve(FIELD, value[1]), walk(value[2], resolve)]
        if len(value) == 3 and value[0] == "metric":
            return ["metric", walk(value[1], resolve), resolve(CARD, value[2])]
        return [walk(item, resolve) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict[str, Any] = {}
    for key, item in value.items():
        if key in KEYS:
            out[key] = resolve(KEYS[key], item)
        elif key == "click_behavior" and isinstance(item, dict):
            out[key] = walk({k: v for k, v in item.items() if k != "targetId"}, resolve)
            if "targetId" in item:
                kind = DASHBOARD if item.get("linkType") == "dashboard" else CARD
                out[key]["targetId"] = resolve(kind, item["targetId"])
        elif key == "link" and isinstance(item, dict):
            entity = item.get("entity")
            if isinstance(entity, dict) and entity.get("model") in LINKED:
                kind = LINKED[entity["model"]]
                entity = {**entity, "id": resolve(kind, entity.get("id"))}
                out[key] = {**item, "entity": entity}
            else:
                out[key] = item
        elif key == "column_settings" and isinstance(item, dict):
            out[key] = {
                _column_key(k, resolve): walk(v, resolve) for k, v in item.items()
            }
        else:
            out[key] = walk(item, resolve)
    return out


def _column_key(key: str, resolve: Resolve) -> str:
    try:
        parsed = json.loads(key)
    except ValueError:
        return key
    return json.dumps(walk(parsed, resolve), separators=(",", ":"))


def click_targets(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "click_behavior" and isinstance(item, dict):
                if isinstance(item.get("targetId"), str):
                    found.add(item["targetId"])
            else:
                found |= click_targets(item)
    elif isinstance(value, list):
        for item in value:
            found |= click_targets(item)
    return found


def without_clicks_to(value: Any, targets: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            k: without_clicks_to(v, targets)
            for k, v in value.items()
            if not (
                k == "click_behavior"
                and isinstance(v, dict)
                and v.get("targetId") in targets
            )
        }
    if isinstance(value, list):
        return [without_clicks_to(v, targets) for v in value]
    return value
