from dataclasses import dataclass
from types import ModuleType
from typing import Any

from pydantic import ValidationError

from metabase_sync.errors import (
    ApiError,
    StateError,
    UnresolvedReferenceError,
    describe,
)
from metabase_sync.format.representations import Item
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.metabase.index import UNRESOLVED, Index, RemoteItem, with_detail


@dataclass
class Remote:
    index: Index
    api: ModuleType

    def refresh(self, client: MetabaseClient, entity_ids: set[str]) -> None:
        hits = [hit for eid in sorted(entity_ids) if (hit := self.index.find(eid))]

        def detail(hit: tuple[str, RemoteItem]) -> dict[str, Any] | None:
            kind, item = hit
            try:
                return client.get(f"{self.api.RESOURCES[kind].ENDPOINT}/{item.id}")
            except ApiError as error:
                if error.status == 404:
                    return None
                raise

        for (kind, item), found in zip(hits, client.map(detail, hits), strict=True):
            if found is None:
                self.index.forget(kind, item.id)
            else:
                summary = self.index.raw[kind, item.id]
                self.index.add(kind, with_detail(summary, found))

    def item(self, kind: str, remote: RemoteItem) -> Item:
        return self.read(kind, self.index.raw[kind, remote.id])

    def read(self, kind: str, raw: dict[str, Any]) -> Item:
        resource = self.api.RESOURCES[kind]
        unresolved: set[tuple[str, Any]] = set()
        token = UNRESOLVED.set(unresolved)
        try:
            item = resource.read(
                resource.Response.model_validate(raw, extra="ignore"), self.index
            )
        except ValidationError as error:
            problems = "; ".join(describe(error))
            raise StateError(
                f"Metabase returned {kind} {raw.get('id')} in a shape "
                f"metabase-sync can't read: {problems}"
            ) from error
        except UnresolvedReferenceError as error:
            raise StateError(
                f"Can't read {kind} {raw.get('id')} {raw.get('name')!r} from "
                f"Metabase: {error}",
                hint=error.hint,
            ) from error
        finally:
            UNRESOLVED.reset(token)
        if unresolved:
            grouped: dict[str, list[Any]] = {}
            for what, value in sorted(unresolved):
                grouped.setdefault(what, []).append(value)
            refs = " and ".join(
                f"{what}{'' if len(ids) == 1 else 's'} {', '.join(map(str, ids))}"
                for what, ids in grouped.items()
            )
            self.index.broken.append(
                f"{kind} {raw.get('name')!r} refers to {refs}, which Metabase "
                "doesn't have"
            )
        return item
