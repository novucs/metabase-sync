from typing import Any, Literal

from pydantic import AliasPath, BaseModel, Field

from metabase_sync.format.representations import COLLECTION, DASHBOARD
from metabase_sync.format.representations.card import Card, ResultColumn
from metabase_sync.metabase.index import Index
from metabase_sync.metabase.references import native, portable

ENDPOINT = "/api/card"
LISTING = {"f": "all"}
CONTAINER = "collection_id"
FIXED = frozenset({"entity_id"})
RESET_WHEN_OMITTED: frozenset[str] = frozenset()


class Column(BaseModel):
    name: str
    base_type: str | None = None
    description: str | None = None
    display_name: str | None = None
    effective_type: str | None = None
    field_ref: list[Any] | None = None
    fk_target_field_id: int | None = None
    semantic_type: str | None = None
    settings: dict[str, Any] | None = None
    visibility_type: str | None = None


class Response(BaseModel):
    id: int
    entity_id: str
    name: str
    description: str | None = None
    collection_id: int | None = None
    dashboard_id: int | None = None
    creator: str | None = Field(None, validation_alias=AliasPath("creator", "email"))
    type: Literal["question", "model", "metric"] = "question"
    display: str
    database_id: int | None = None
    dataset_query: dict[str, Any]
    parameters: list[Any] | None = None
    parameter_mappings: list[Any] | None = None
    visualization_settings: dict[str, Any] = {}
    collection_position: int | None = None
    collection_preview: bool = True
    enable_embedding: bool = False
    embedding_type: str | None = None
    embedding_params: dict[str, Any] | None = None
    result_metadata: list[Column] | None = None


class Request(BaseModel):
    entity_id: str
    name: str
    description: str | None
    collection_id: int | None
    dashboard_id: int | None
    type: Literal["question", "model", "metric"]
    display: str
    dataset_query: dict[str, Any]
    parameters: list[Any]
    parameter_mappings: list[Any]
    visualization_settings: dict[str, Any]
    collection_position: int | None
    collection_preview: bool
    enable_embedding: bool
    embedding_type: str | None
    embedding_params: dict[str, Any] | None
    result_metadata: list[dict[str, Any]] | None = Field(
        None, exclude_if=lambda columns: columns is None
    )


def read(response: Response, ids: Index) -> Card:
    database = (
        ids.lenient(ids.database_name, response.database_id, "database id")
        if response.database_id is not None
        else None
    )
    return Card(
        name=response.name,
        entity_id=response.entity_id,
        description=response.description,
        collection_id=(
            ids.entity_id(COLLECTION, response.collection_id)
            if response.collection_id is not None
            else None
        ),
        dashboard_id=(
            ids.entity_id(DASHBOARD, response.dashboard_id)
            if response.dashboard_id is not None
            else None
        ),
        creator_id=response.creator,
        type=response.type,
        display=response.display,
        database_id=database if isinstance(database, str) else None,
        dataset_query=portable(response.dataset_query, ids),
        parameters=portable(response.parameters or [], ids),
        parameter_mappings=portable(response.parameter_mappings or [], ids),
        visualization_settings=portable(response.visualization_settings, ids),
        collection_position=response.collection_position,
        collection_preview=response.collection_preview,
        enable_embedding=response.enable_embedding,
        embedding_type=response.embedding_type,
        embedding_params=response.embedding_params,
        result_metadata=(
            [read_column(column, ids) for column in response.result_metadata]
            if response.type == "model" and response.result_metadata is not None
            else None
        ),
    )


def read_column(column: Column, ids: Index) -> ResultColumn:
    return ResultColumn(
        name=column.name,
        base_type=column.base_type,
        description=column.description,
        display_name=column.display_name,
        effective_type=column.effective_type,
        field_ref=portable(column.field_ref, ids),
        fk_target_field_id=(
            ids.lenient(ids.metadata.field_ref, column.fk_target_field_id, "field id")
            if column.fk_target_field_id is not None
            else None
        ),
        semantic_type=column.semantic_type,
        settings=column.settings,
        visibility_type=column.visibility_type,
    )


def request(card: Card, ids: Index) -> Request:
    return Request(
        entity_id=card.entity_id,
        name=card.name,
        description=card.description,
        collection_id=(
            ids.id(COLLECTION, card.collection_id) if card.collection_id else None
        ),
        dashboard_id=ids.id(DASHBOARD, card.dashboard_id)
        if card.dashboard_id
        else None,
        type=card.type,
        display=card.display,
        dataset_query=native(card.dataset_query, ids),
        parameters=native(card.parameters, ids),
        parameter_mappings=native(card.parameter_mappings, ids),
        visualization_settings=native(card.visualization_settings, ids),
        collection_position=card.collection_position,
        collection_preview=card.collection_preview,
        enable_embedding=card.enable_embedding,
        embedding_type=card.embedding_type,
        embedding_params=card.embedding_params,
        result_metadata=(
            [
                request_column(column, ids).model_dump(exclude_none=True)
                for column in card.result_metadata
            ]
            if card.result_metadata is not None
            else None
        ),
    )


def request_column(column: ResultColumn, ids: Index) -> Column:
    return Column(
        name=column.name,
        base_type=column.base_type,
        description=column.description,
        display_name=column.display_name,
        effective_type=column.effective_type,
        field_ref=native(column.field_ref, ids),
        fk_target_field_id=(
            ids.metadata.field_id(column.fk_target_field_id)
            if isinstance(column.fk_target_field_id, list)
            else column.fk_target_field_id
        ),
        semantic_type=column.semantic_type,
        settings=column.settings,
        visibility_type=column.visibility_type,
    )
