from typing import Any, Literal

from pydantic import AliasPath, BaseModel, Field

from metabase_sync.format.representations import CARD, COLLECTION
from metabase_sync.format.representations.dashboard import (
    Dashboard,
    Dashcard,
    Series,
    Tab,
)
from metabase_sync.metabase.index import Index, RemoteItem
from metabase_sync.metabase.references import native, portable

ENDPOINT = "/api/dashboard"
LISTING = {"f": "all"}
CONTAINER = "collection_id"
FIXED: frozenset[str] = frozenset()


class TabResponse(BaseModel):
    id: int
    entity_id: str
    name: str
    position: int


class SeriesCard(BaseModel):
    id: int


class DashcardResponse(BaseModel):
    id: int
    entity_id: str
    dashboard_tab_id: int | None = None
    card_id: int | None = None
    row: int
    col: int
    size_x: int
    size_y: int
    parameter_mappings: list[Any] | None = None
    inline_parameters: list[str] | None = None
    series: list[SeriesCard] = []
    visualization_settings: dict[str, Any] = {}


class Response(BaseModel):
    id: int
    entity_id: str
    name: str
    description: str | None = None
    collection_id: int | None = None
    creator: str | None = Field(None, validation_alias=AliasPath("creator", "email"))
    width: Literal["fixed", "full"] = "fixed"
    auto_apply_filters: bool = True
    collection_position: int | None = None
    enable_embedding: bool = False
    embedding_type: str | None = None
    embedding_params: dict[str, Any] | None = None
    parameters: list[Any] | None = None
    tabs: list[TabResponse] = []
    dashcards: list[DashcardResponse] = []


class Request(BaseModel):
    name: str
    description: str | None
    collection_id: int | None
    width: Literal["fixed", "full"]
    auto_apply_filters: bool
    collection_position: int | None
    enable_embedding: bool
    embedding_type: str | None
    embedding_params: dict[str, Any] | None


class TabRequest(BaseModel):
    id: int
    name: str
    position: int


class DashcardRequest(BaseModel):
    id: int
    dashboard_tab_id: int | None
    card_id: int | None
    row: int
    col: int
    size_x: int
    size_y: int
    parameter_mappings: list[Any]
    inline_parameters: list[str] | None
    series: list[SeriesCard]
    visualization_settings: dict[str, Any]


class ContentsRequest(BaseModel):
    parameters: list[Any]
    tabs: list[TabRequest]
    dashcards: list[DashcardRequest]


def read(response: Response, ids: Index) -> Dashboard:
    return Dashboard(
        name=response.name,
        entity_id=response.entity_id,
        description=response.description,
        collection_id=(
            ids.entity_id(COLLECTION, response.collection_id)
            if response.collection_id is not None
            else None
        ),
        creator_id=response.creator,
        width=response.width,
        auto_apply_filters=response.auto_apply_filters,
        collection_position=response.collection_position,
        enable_embedding=response.enable_embedding,
        embedding_type=response.embedding_type,
        embedding_params=response.embedding_params,
        parameters=portable(response.parameters or [], ids),
        tabs=[
            Tab(entity_id=tab.entity_id, name=tab.name, position=tab.position)
            for tab in response.tabs
        ],
        dashcards=[read_dashcard(dashcard, ids) for dashcard in response.dashcards],
    )


def read_dashcard(dashcard: DashcardResponse, ids: Index) -> Dashcard:
    return Dashcard(
        entity_id=dashcard.entity_id,
        dashboard_tab_id=(
            ids.tab_pair(dashcard.dashboard_tab_id)
            if dashcard.dashboard_tab_id is not None
            else None
        ),
        card_id=(
            ids.entity_id(CARD, dashcard.card_id)
            if dashcard.card_id is not None
            else None
        ),
        row=dashcard.row,
        col=dashcard.col,
        size_x=dashcard.size_x,
        size_y=dashcard.size_y,
        parameter_mappings=portable(dashcard.parameter_mappings or [], ids),
        inline_parameters=dashcard.inline_parameters,
        series=[
            Series(card_id=ids.entity_id(CARD, card.id), position=position)
            for position, card in enumerate(dashcard.series)
        ],
        visualization_settings=portable(dashcard.visualization_settings, ids),
    )


def request(dashboard: Dashboard, ids: Index) -> Request:
    return Request(
        name=dashboard.name,
        description=dashboard.description,
        collection_id=(
            ids.id(COLLECTION, dashboard.collection_id)
            if dashboard.collection_id
            else None
        ),
        width=dashboard.width,
        auto_apply_filters=dashboard.auto_apply_filters,
        collection_position=dashboard.collection_position,
        enable_embedding=dashboard.enable_embedding,
        embedding_type=dashboard.embedding_type,
        embedding_params=dashboard.embedding_params,
    )


def contents(dashboard: Dashboard, ids: Index, current: RemoteItem) -> ContentsRequest:
    current_tabs = {t.entity_id: t.id for t in current.tabs}
    current_dashcards = {dc.entity_id: dc.id for dc in current.dashcards}
    tab_ids: dict[str, int] = {}
    for tab in dashboard.tabs:
        tab_ids[tab.entity_id] = current_tabs.get(tab.entity_id) or -(len(tab_ids) + 1)
    return ContentsRequest(
        parameters=native(dashboard.parameters, ids),
        tabs=[
            TabRequest(id=tab_ids[tab.entity_id], name=tab.name, position=tab.position)
            for tab in dashboard.tabs
        ],
        dashcards=[
            DashcardRequest(
                id=current_dashcards.get(dashcard.entity_id) or -(position + 1),
                dashboard_tab_id=tab_ids.get(dashcard.tab) if dashcard.tab else None,
                card_id=ids.id(CARD, dashcard.card_id) if dashcard.card_id else None,
                row=dashcard.row,
                col=dashcard.col,
                size_x=dashcard.size_x,
                size_y=dashcard.size_y,
                parameter_mappings=native(dashcard.parameter_mappings, ids),
                inline_parameters=dashcard.inline_parameters,
                series=[
                    SeriesCard(id=ids.id(CARD, s.card_id)) for s in dashcard.series
                ],
                visualization_settings=native(dashcard.visualization_settings, ids),
            )
            for position, dashcard in enumerate(dashboard.dashcards)
        ],
    )
