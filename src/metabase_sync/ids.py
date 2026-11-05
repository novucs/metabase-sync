import re
import secrets
import string
from typing import Annotated, Any, TypeGuard

from pydantic import StringConstraints

_ALPHABET = string.ascii_letters + string.digits + "_-"
PATTERN = r"^[A-Za-z0-9_-]{21}$"
_PATTERN = re.compile(PATTERN)

EntityId = Annotated[str, StringConstraints(pattern=PATTERN)]


def new_entity_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(21))


def is_entity_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(_PATTERN.match(value))


def swap_ids(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: swap_ids(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [swap_ids(v, mapping) for v in value]
    if isinstance(value, str) and value in mapping:
        return mapping[value]
    return value
