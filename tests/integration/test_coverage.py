from typing import Any, get_args

import pytest
from pydantic import BaseModel

from metabase_sync.store.tree import read_tree

from ._content import build_rich
from .test_roundtrip import export, scope

pytestmark = pytest.mark.integration

NOT_SET = {
    "Collection.authority_level": "official collections need Metabase Enterprise",
    "Subscription.disable_links": "Metabase sets it only for embedded requests",
    "Channel.details": "only Slack and webhook channels use it; tests have neither",
}


def _set_fields(value: Any, found: dict[str, set[str]]) -> None:
    if isinstance(value, BaseModel):
        for name, field in type(value).model_fields.items():
            item = getattr(value, name)
            if item != field.get_default(call_default_factory=True):
                found.setdefault(type(value).__name__, set()).add(name)
            _set_fields(item, found)
    elif isinstance(value, list):
        for item in value:
            _set_fields(item, found)


def _nested(model: type[BaseModel]) -> set[type[BaseModel]]:
    out = {model}
    for field in model.model_fields.values():
        for inner in [field.annotation, *get_args(field.annotation)]:
            for candidate in [inner, *get_args(inner)]:
                if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                    out |= _nested(candidate)
    return out


def test_the_round_trip_content_sets_every_field_we_manage(client, mb, tmp_path):
    rich = build_rich(mb)
    root = scope(tmp_path / "root", rich.collection["entity_id"])
    export(client, root)

    found: dict[str, set[str]] = {}
    models: set[type[BaseModel]] = set()
    for entity in read_tree(root).entities.values():
        _set_fields(entity.data, found)
        models |= _nested(type(entity.data))

    every = {f"{m.__name__}.{name}" for m in models for name in m.model_fields}
    covered = {f"{model}.{name}" for model, names in found.items() for name in names}

    missing = every - covered
    assert missing == set(NOT_SET), " ".join(sorted(missing ^ set(NOT_SET)))
