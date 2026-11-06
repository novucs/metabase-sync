from typing import Any

from pydantic import BaseModel

from metabase_sync.format.representations import DASHBOARD
from metabase_sync.format.representations.subscription import (
    Attachment,
    Channel,
    Subscription,
)
from metabase_sync.metabase.index import Index, RemoteItem
from metabase_sync.metabase.references import native, portable

ENDPOINT = "/api/pulse"
LISTING: dict[str, str] = {}
CONTAINER = "dashboard_id"
FIXED = frozenset({"dashboard_id"})
RESET_WHEN_OMITTED = frozenset({"skip_if_empty", "channels"})


class User(BaseModel):
    email: str


class ChannelResponse(BaseModel):
    channel_type: str
    enabled: bool = True
    schedule_type: str | None = None
    schedule_hour: int | None = None
    schedule_day: str | None = None
    schedule_frame: str | None = None
    details: dict[str, Any] | None = None
    recipients: list[User] = []


class CardResponse(BaseModel):
    dashboard_card_id: int | None
    include_csv: bool = False
    include_xls: bool = False
    format_rows: bool = True
    pivot_results: bool = False


class Response(BaseModel):
    id: int
    entity_id: str
    dashboard_id: int
    skip_if_empty: bool = False
    disable_links: bool = False
    parameters: list[Any] | None = None
    channels: list[ChannelResponse]
    cards: list[CardResponse] = []


class UserRecipient(BaseModel):
    id: int


class EmailRecipient(BaseModel):
    email: str


class ChannelRequest(BaseModel):
    channel_type: str
    enabled: bool
    schedule_type: str | None
    schedule_hour: int | None
    schedule_day: str | None
    schedule_frame: str | None
    details: dict[str, Any] | None
    recipients: list[UserRecipient | EmailRecipient]


class CardRequest(CardResponse):
    id: int


class Request(BaseModel):
    dashboard_id: int
    name: str | None
    skip_if_empty: bool
    parameters: list[Any]
    channels: list[ChannelRequest]
    cards: list[CardRequest]


def read(response: Response, ids: Index) -> Subscription:
    return Subscription(
        entity_id=response.entity_id,
        dashboard_id=ids.entity_id(DASHBOARD, response.dashboard_id),
        skip_if_empty=response.skip_if_empty,
        disable_links=response.disable_links,
        parameters=portable(response.parameters or [], ids),
        channels=[
            Channel(
                channel_type=channel.channel_type,
                enabled=channel.enabled,
                schedule_type=channel.schedule_type,
                schedule_hour=channel.schedule_hour,
                schedule_day=channel.schedule_day,
                schedule_frame=channel.schedule_frame,
                details=channel.details,
                recipients=[user.email for user in channel.recipients],
            )
            for channel in response.channels
        ],
        attachments=[
            Attachment(
                dashcard_id=ids.dashcard_entity_id(card.dashboard_card_id),
                include_csv=card.include_csv,
                include_xls=card.include_xls,
                format_rows=card.format_rows,
                pivot_results=card.pivot_results,
            )
            for card in response.cards
            if card.dashboard_card_id is not None
            and (card.include_csv or card.include_xls)
        ],
    )


def request(subscription: Subscription, ids: Index, dashboard: RemoteItem) -> Request:
    attached = {a.dashcard_id: a for a in subscription.attachments}
    return Request(
        dashboard_id=dashboard.id,
        name=dashboard.name,
        skip_if_empty=subscription.skip_if_empty,
        parameters=native(subscription.parameters, ids),
        channels=[
            ChannelRequest(
                channel_type=channel.channel_type,
                enabled=channel.enabled,
                schedule_type=channel.schedule_type,
                schedule_hour=channel.schedule_hour,
                schedule_day=channel.schedule_day,
                schedule_frame=channel.schedule_frame,
                details=channel.details,
                recipients=[
                    UserRecipient(id=user_id)
                    if (user_id := ids.user_id(email)) is not None
                    else EmailRecipient(email=email)
                    for email in channel.recipients
                ],
            )
            for channel in subscription.channels
        ],
        cards=[
            CardRequest(
                id=dashcard.card_id,
                dashboard_card_id=dashcard.id,
                **(
                    attached[dashcard.entity_id].model_dump(exclude={"dashcard_id"})
                    if dashcard.entity_id in attached
                    else {}
                ),
            )
            for dashcard in dashboard.dashcards
            if dashcard.card_id
        ],
    )
