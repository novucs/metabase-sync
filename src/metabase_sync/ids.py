import re
import secrets
from typing import Annotated, Any, TypeGuard

from pydantic import StringConstraints

PATTERN = r"^[A-Za-z0-9_-]{21}$"

EntityId = Annotated[str, StringConstraints(pattern=PATTERN)]


def new_entity_id() -> str:
    return secrets.token_urlsafe(16)[:21]


def is_entity_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(PATTERN, value) is not None


def swap_ids(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: swap_ids(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [swap_ids(v, mapping) for v in value]
    if isinstance(value, str) and value in mapping:
        return mapping[value]
    return value
