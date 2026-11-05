from types import ModuleType

from metabase_sync.format.api.v58 import (
    card,
    collection,
    dashboard,
    snippet,
    subscription,
)
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    SNIPPET,
    SUBSCRIPTION,
)

RESOURCES: dict[str, ModuleType] = {
    COLLECTION: collection,
    SNIPPET: snippet,
    CARD: card,
    DASHBOARD: dashboard,
    SUBSCRIPTION: subscription,
}
