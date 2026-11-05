import hashlib
import json
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, computed_field

from metabase_sync.format import representations
from metabase_sync.format.representations import DASHBOARD
from metabase_sync.store.tree import Entity

CREATE = "create"
IMPORT = "import"
UPDATE = "update"
RESTORE = "restore"
ARCHIVE = "archive"
DELETE = "delete"
POSITIVE = (CREATE, RESTORE, UPDATE)
REMOVALS = (ARCHIVE, DELETE)

BASE = "base"
CONTENTS = "contents"
LINKS = "links"
CONTENT_FIELDS = ("parameters", "tabs", "dashcards")

SAME = "same"
OURS = "ours"
THEIRS = "theirs"
CONFLICT = "conflict"
UNMANAGED = "unmanaged"


class Key(NamedTuple):
    entity_id: str
    part: str
    action: str


class Operation(BaseModel):
    action: str
    kind: str
    entity_id: str
    name: str
    path: str | None
    part: str = BASE
    changes: list[str] = []
    depends_on: list[str] = []
    remote_hash: str | None = None
    local_hash: str | None = None
    reason: str | None = None
    deferred: list[str] = []

    @property
    def key(self) -> Key:
        return Key(self.entity_id, self.part, self.action)

    @computed_field
    @property
    def id(self) -> str:
        return ":".join(self.key)


class Ref(BaseModel):
    kind: str
    entity_id: str
    name: str


class Drift(Ref):
    path: str = ""
    change: str


class Plan(BaseModel):
    format: Literal[3] = 3
    metabase: str
    force: bool = False
    operations: list[Operation] = []
    renames: dict[str, str] = {}
    conflicts: list[Drift] = []
    drift: list[Drift] = []
    diffs: dict[str, str] = {}
    unmanaged: list[Ref] = []
    cascaded: list[Ref] = []

    @computed_field
    @property
    def fingerprint(self) -> str:
        payload = self.model_dump_json(
            include={"force", "operations", "renames", "conflicts"}
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def empty(self) -> bool:
        return not self.operations

    @property
    def drifted(self) -> set[str]:
        return {i.entity_id for i in [*self.drift, *self.conflicts]}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def local_hash(entity: Entity) -> str:
    return digest(representations.comparable(entity.data))


def classify(prior: str | None, local: str | None, remote: str | None) -> str:
    if local == remote:
        return SAME
    if prior is None:
        return UNMANAGED
    if local == prior:
        return THEIRS
    if remote == prior:
        return OURS
    return CONFLICT


def part_view(kind: str, part: str, entity: dict[str, Any]) -> dict[str, Any]:
    if kind != DASHBOARD:
        return entity
    if part == CONTENTS:
        return {k: entity.get(k) for k in CONTENT_FIELDS}
    return {k: v for k, v in entity.items() if k not in CONTENT_FIELDS}
