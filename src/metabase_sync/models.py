"""Pydantic models for the Metabase wire types we read and push back.

All models accept extra fields (the server returns 50+ fields per card; we only
care about a fraction). When we POST/PUT back, we only send the fields we know
matter, so unknown server-side fields never leak into write paths.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JsonDict = dict[str, Any]


class _Wire(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Collection(_Wire):
    id: int | Literal["root"]
    entity_id: str | None = None
    name: str
    description: str | None = None
    parent_id: int | None = None
    location: str | None = None
    is_personal: bool = False
    personal_owner_id: int | None = None
    archived: bool = False
    authority_level: str | None = None


class Card(_Wire):
    id: int
    entity_id: str
    name: str
    description: str | None = None
    type: Literal["question", "model", "metric"] = "question"
    display: str
    database_id: int | None = None
    collection_id: int | None = None
    dashboard_id: int | None = None
    archived: bool = False
    enable_embedding: bool = False
    embedding_params: JsonDict | None = None
    cache_ttl: int | None = None
    parameters: list[JsonDict] = Field(default_factory=list)
    visualization_settings: JsonDict = Field(default_factory=dict)
    legacy_query: str | None = None
    dataset_query: JsonDict = Field(default_factory=dict)
    # Column-level metadata: descriptions, semantic types, FK refs. For models
    # these are heavily curated by users and must be preserved across applies.
    result_metadata: list[JsonDict] | None = None


class Tab(_Wire):
    id: int
    entity_id: str | None = None
    name: str
    position: int


class Dashcard(_Wire):
    id: int
    entity_id: str | None = None
    card_id: int | None = None
    dashboard_tab_id: int | None = None
    row: int
    col: int
    size_x: int
    size_y: int
    parameter_mappings: list[JsonDict] = Field(default_factory=list)
    visualization_settings: JsonDict = Field(default_factory=dict)
    series: list[JsonDict] = Field(default_factory=list)
    inline_parameters: list[JsonDict] | None = None
    action_id: int | None = None


class Dashboard(_Wire):
    id: int
    entity_id: str
    name: str
    description: str | None = None
    collection_id: int | None = None
    archived: bool = False
    auto_apply_filters: bool = True
    cache_ttl: int | None = None
    embedding_params: JsonDict | None = None
    enable_embedding: bool = False
    position: int | None = None
    width: str | None = None
    parameters: list[JsonDict] = Field(default_factory=list)
    tabs: list[Tab] = Field(default_factory=list)
    dashcards: list[Dashcard] = Field(default_factory=list)


class Snippet(_Wire):
    id: int
    entity_id: str
    name: str
    description: str | None = None
    content: str
    template_tags: JsonDict | None = Field(default_factory=dict)
    collection_id: int | None = None
    archived: bool = False


class Database(_Wire):
    id: int
    entity_id: str | None = None
    name: str
    engine: str


class PulseCard(_Wire):
    id: int
    dashboard_card_id: int | None = None
    name: str | None = None
    display: str | None = None
    include_csv: bool = False
    include_xls: bool = False
    format_rows: bool = True
    pivot_results: bool = False
    parameter_mappings: list[JsonDict] | None = None


class PulseRecipient(_Wire):
    id: int | None = None
    email: str | None = None


class PulseChannel(_Wire):
    entity_id: str | None = None
    channel_type: Literal["email", "slack", "http"]
    schedule_type: str
    schedule_hour: int | None = None
    schedule_day: str | None = None
    schedule_frame: str | None = None
    recipients: list[PulseRecipient] = Field(default_factory=list)
    enabled: bool = True
    channel_id: int | None = None


class Pulse(_Wire):
    id: int
    entity_id: str
    name: str
    collection_id: int | None = None
    dashboard_id: int | None = None
    skip_if_empty: bool = False
    disable_links: bool = False
    archived: bool = False
    parameters: list[JsonDict] = Field(default_factory=list)
    cards: list[PulseCard] = Field(default_factory=list)
    channels: list[PulseChannel] = Field(default_factory=list)
