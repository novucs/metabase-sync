from pydantic import BaseModel, Field

from metabase_sync.ids import EntityId


class Snippet(BaseModel):
    name: str = Field(min_length=1)
    entity_id: EntityId
    description: str | None = None
    creator_id: str | None = None
    content: str = Field(min_length=1)
