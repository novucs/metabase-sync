from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from metabase_sync.format.representations.content import link_identity, sort_keys
from metabase_sync.ids import EntityId


class Tab(BaseModel):
    entity_id: EntityId
    name: str
    position: int = Field(ge=0)


class Series(BaseModel):
    card_id: EntityId
    position: int = Field(ge=0)


class Dashcard(BaseModel):
    entity_id: EntityId
    dashboard_tab_id: (
        Annotated[list[EntityId], Field(min_length=2, max_length=2)] | None
    ) = None
    card_id: EntityId | None = None
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    size_x: int
    size_y: int
    parameter_mappings: list[Any] = []
    inline_parameters: list[str] | None = None
    series: list[Series] = []
    visualization_settings: dict[str, Any] = {}

    @property
    def tab(self) -> str | None:
        return self.dashboard_tab_id[1] if self.dashboard_tab_id else None

    @field_validator("parameter_mappings")
    @classmethod
    def sorted_keys(cls, value: Any) -> Any:
        return sort_keys(value)

    @field_validator("series")
    @classmethod
    def by_position(cls, series: list[Series]) -> list[Series]:
        return sorted(series, key=lambda s: s.position)

    @field_validator("visualization_settings")
    @classmethod
    def link_identity(cls, settings: dict[str, Any]) -> dict[str, Any]:
        return link_identity(sort_keys(settings))


class Dashboard(BaseModel):
    name: str = Field(min_length=1)
    entity_id: EntityId
    description: str | None = None
    collection_id: EntityId | None = None
    creator_id: str | None = None
    width: Literal["fixed", "full"] = "fixed"
    auto_apply_filters: bool = True
    collection_position: int | None = None
    enable_embedding: bool = False
    embedding_type: str | None = None
    embedding_params: dict[str, Any] | None = None
    parameters: list[Any] = []
    tabs: list[Tab] = []
    dashcards: list[Dashcard] = []

    @field_validator("embedding_params", "parameters")
    @classmethod
    def sorted_keys(cls, value: Any) -> Any:
        return sort_keys(value)

    @field_validator("tabs")
    @classmethod
    def by_position(cls, tabs: list[Tab]) -> list[Tab]:
        return sorted(tabs, key=lambda t: (t.position, t.entity_id))

    @model_validator(mode="after")
    def dashcards_on_tabs(self) -> Self:
        positions: dict[str | None, int] = {t.entity_id: t.position for t in self.tabs}
        for dc in self.dashcards:
            where = f"the dashcard at row {dc.row}, column {dc.col}"
            if dc.dashboard_tab_id and (
                dc.dashboard_tab_id[0] != self.entity_id or dc.tab not in positions
            ):
                raise ValueError(f"{where} is on a tab this dashboard doesn't have")
            if positions and not dc.dashboard_tab_id:
                raise ValueError(
                    f"{where} needs a dashboard_tab_id, because the dashboard has tabs"
                )
        self.dashcards = sorted(
            self.dashcards,
            key=lambda dc: (positions.get(dc.tab, -1), dc.row, dc.col, dc.entity_id),
        )
        return self
