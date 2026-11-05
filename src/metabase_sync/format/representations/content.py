import uuid
from typing import Any

UUID_NAMESPACE = uuid.UUID("5f1b8c2e-6a4d-4c3b-9e7f-0d2a1b3c4e5f")
LINK_IDENTITY = ("id", "model")


def sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: sort_keys(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [sort_keys(v) for v in value]
    return value


def link_identity(settings: dict[str, Any]) -> dict[str, Any]:
    link = settings.get("link")
    if not isinstance(link, dict) or not isinstance(link.get("entity"), dict):
        return settings
    entity = {k: link["entity"][k] for k in LINK_IDENTITY if k in link["entity"]}
    return {**settings, "link": {**link, "entity": entity}}


def is_aggregation_ref(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and value[0] == "aggregation"
        and isinstance(value[2], str)
    )


def defined_uuids(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [value["lib/uuid"]] if isinstance(value.get("lib/uuid"), str) else []
        return own + [u for key in sorted(value) for u in defined_uuids(value[key])]
    if isinstance(value, list):
        return [u for item in value for u in defined_uuids(item)]
    return []


def aggregation_refs(value: Any) -> set[str]:
    if is_aggregation_ref(value):
        return {value[2]}
    if isinstance(value, dict):
        return {u for item in value.values() for u in aggregation_refs(item)}
    if isinstance(value, list):
        return {u for item in value for u in aggregation_refs(item)}
    return set()


def renumbered(value: Any, names: dict[str, str]) -> Any:
    if is_aggregation_ref(value):
        return [value[0], renumbered(value[1], names), names.get(value[2], value[2])]
    if isinstance(value, dict):
        return {
            k: names[v] if k == "lib/uuid" else renumbered(v, names)
            for k, v in value.items()
            if k != "lib/uuid" or v in names
        }
    if isinstance(value, list):
        return [renumbered(item, names) for item in value]
    return value


def stable_uuids(fields: dict[str, Any]) -> dict[str, Any]:
    referenced = aggregation_refs(fields)
    kept = [
        u
        for u in dict.fromkeys(defined_uuids(fields.get("dataset_query")))
        if u in referenced
    ]
    names = {old: str(uuid.uuid5(UUID_NAMESPACE, str(i))) for i, old in enumerate(kept)}
    return {k: renumbered(v, names) for k, v in fields.items()}
