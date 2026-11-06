from typing import Literal

from pydantic import BaseModel, Field, field_validator

from metabase_sync.format.representations import COLLECTION
from metabase_sync.format.representations.collection import Collection
from metabase_sync.metabase.index import Index, location_ids

ENDPOINT = "/api/collection"
LISTING = {"archived": "false"}
CONTAINER = "parent_id"
FIXED: frozenset[str] = frozenset()
RESET_WHEN_OMITTED: frozenset[str] = frozenset()


class Response(BaseModel):
    id: int
    entity_id: str
    name: str
    description: str | None = None
    parent_id: int | None = Field(None, validation_alias="location")
    authority_level: Literal["official"] | None = None

    @field_validator("parent_id", mode="before")
    @classmethod
    def last_in_location(cls, location: str | None) -> int | None:
        ancestry = location_ids(location)
        return ancestry[-1] if ancestry else None


class Request(BaseModel):
    name: str
    description: str | None
    parent_id: int | None
    authority_level: Literal["official"] | None


def read(response: Response, ids: Index) -> Collection:
    return Collection(
        name=response.name,
        entity_id=response.entity_id,
        description=response.description,
        parent_id=(
            ids.entity_id(COLLECTION, response.parent_id)
            if response.parent_id is not None
            else None
        ),
        authority_level=response.authority_level,
    )


def request(collection: Collection, ids: Index) -> Request:
    return Request(
        name=collection.name,
        description=collection.description,
        parent_id=(
            ids.id(COLLECTION, collection.parent_id) if collection.parent_id else None
        ),
        authority_level=collection.authority_level,
    )
