import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, RootModel, ValidationError

from metabase_sync.store.tree import WORKDIR


class _Entry(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Started(_Entry):
    status: Literal["started"] = "started"
    op: str
    action: str
    kind: str
    entity_id: str
    name: str


class Done(_Entry):
    status: Literal["done"] = "done"
    op: str


class Posting(_Entry):
    status: Literal["posting"] = "posting"
    op: str
    kind: str
    entity_id: str
    name: str | None
    container: int | None
    existing: list[int]


class Created(_Entry):
    status: Literal["created"] = "created"
    op: str
    kind: str
    entity_id: str
    created_id: int
    created_entity_id: str | None


class Renamed(_Entry):
    status: Literal["renamed"] = "renamed"
    mapping: dict[str, str]


Entry = Started | Done | Posting | Created | Renamed


class _Line(RootModel[Entry]):
    root: Entry = Field(discriminator="status")


class Journal:
    def __init__(self, root: Path) -> None:
        self.path = root / WORKDIR / "journal.jsonl"
        self._lock = threading.Lock()

    def entries(self) -> list[Entry]:
        if not self.path.exists():
            return []
        out: list[Entry] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(_Line.model_validate_json(line).root)
            except ValidationError:
                break
        return out

    def record(self, entry: Entry) -> None:
        line = entry.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
        try:
            self.path.parent.rmdir()
        except OSError:
            pass
