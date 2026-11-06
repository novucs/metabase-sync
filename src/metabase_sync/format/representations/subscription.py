import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from metabase_sync.format.representations.content import SortedKeys
from metabase_sync.ids import EntityId


class Channel(BaseModel):
    channel_type: str
    enabled: bool = True
    schedule_type: str | None = None
    schedule_hour: int | None = None
    schedule_day: str | None = None
    schedule_frame: str | None = None
    details: dict[str, Any] | None = None
    recipients: list[str] = []

    @field_validator("recipients")
    @classmethod
    def lowercase_and_sorted(cls, emails: list[str]) -> list[str]:
        return sorted({e.lower() for e in emails})


class Attachment(BaseModel):
    dashcard_id: EntityId
    include_csv: bool = False
    include_xls: bool = False
    format_rows: bool = True
    pivot_results: bool = False


class Subscription(BaseModel):
    entity_id: EntityId
    dashboard_id: EntityId
    skip_if_empty: bool = False
    disable_links: bool = False
    parameters: SortedKeys[list[Any]] = []
    channels: list[Channel] = Field(min_length=1)
    attachments: list[Attachment] = []

    @field_validator("channels")
    @classmethod
    def by_type_and_settings(cls, channels: list[Channel]) -> list[Channel]:
        return sorted(
            channels,
            key=lambda c: (c.channel_type, json.dumps(c.model_dump(), sort_keys=True)),
        )

    @field_validator("attachments")
    @classmethod
    def files_only(cls, attachments: list[Attachment]) -> list[Attachment]:
        return sorted(
            (a for a in attachments if a.include_csv or a.include_xls),
            key=lambda a: a.dashcard_id,
        )
