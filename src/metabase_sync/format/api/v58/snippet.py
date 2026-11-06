from pydantic import AliasPath, BaseModel, Field

from metabase_sync.format.representations.snippet import Snippet
from metabase_sync.metabase.index import Index

ENDPOINT = "/api/native-query-snippet"
LISTING: dict[str, str] = {}
CONTAINER = None
FIXED: frozenset[str] = frozenset()
RESET_WHEN_OMITTED: frozenset[str] = frozenset()


class Response(BaseModel):
    id: int
    entity_id: str
    name: str
    description: str | None = None
    creator: str | None = Field(None, validation_alias=AliasPath("creator", "email"))
    content: str


class Request(BaseModel):
    name: str
    description: str | None
    content: str


def read(response: Response, ids: Index) -> Snippet:
    return Snippet(
        name=response.name,
        entity_id=response.entity_id,
        description=response.description,
        creator_id=response.creator,
        content=response.content,
    )


def request(snippet: Snippet, ids: Index) -> Request:
    return Request(
        name=snippet.name,
        description=snippet.description,
        content=snippet.content,
    )
