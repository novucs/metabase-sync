from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from metabase_sync.format.representations.content import (
    SortedKeys,
    sort_keys,
    stable_uuids,
)
from metabase_sync.ids import EntityId

AUTHORED = (
    "dataset_query",
    "parameters",
    "parameter_mappings",
    "visualization_settings",
)


class ResultColumn(BaseModel):
    name: str
    base_type: str | None = None
    description: str | None = None
    display_name: str | None = None
    effective_type: str | None = None
    field_ref: SortedKeys[list[Any] | None] = None
    fk_target_field_id: list[Any] | int | None = None
    semantic_type: str | None = None
    settings: SortedKeys[dict[str, Any] | None] = None
    visibility_type: str | None = None


class Card(BaseModel):
    name: str = Field(min_length=1)
    entity_id: EntityId
    description: str | None = None
    collection_id: EntityId | None = None
    dashboard_id: EntityId | None = None
    creator_id: str | None = None
    type: Literal["question", "model", "metric"] = "question"
    display: str = Field(min_length=1)
    database_id: str | None = None
    dataset_query: dict[str, Any]
    parameters: list[Any] = []
    parameter_mappings: list[Any] = []
    visualization_settings: dict[str, Any]
    collection_position: int | None = None
    collection_preview: bool = True
    enable_embedding: bool = False
    embedding_type: str | None = None
    embedding_params: SortedKeys[dict[str, Any] | None] = None
    result_metadata: list[ResultColumn] | None = None

    @model_validator(mode="after")
    def stable_query_uuids(self) -> Self:
        for name, value in stable_uuids(
            {name: getattr(self, name) for name in AUTHORED}
        ).items():
            setattr(self, name, sort_keys(value))
        return self
