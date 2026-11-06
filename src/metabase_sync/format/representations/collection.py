from typing import Literal

from pydantic import BaseModel, Field

from metabase_sync.ids import EntityId


class Collection(BaseModel):
    name: str = Field(min_length=1)
    entity_id: EntityId
    description: str | None = None
    parent_id: EntityId | None = None
    authority_level: Literal["official"] | None = None
