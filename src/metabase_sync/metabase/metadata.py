import threading
from typing import Any

from pydantic import BaseModel, Field

from metabase_sync.errors import ApiError, UnresolvedReferenceError
from metabase_sync.metabase.client import MetabaseClient

type TableRef = tuple[str, str | None, str]
type FieldRef = tuple[str | None, ...]


class FieldMeta(BaseModel):
    id: int
    name: str
    parent_id: int | None = None


class TableMeta(BaseModel):
    id: int
    name: str
    schema_name: str | None = Field(None, validation_alias="schema")
    fields: list[FieldMeta] | None = None


class DatabaseMeta(BaseModel):
    name: str
    tables: list[TableMeta] | None = None


class Located(BaseModel):
    db_id: int | None = None
    table_id: int | None = None
    table: Located | None = None


class Metadata:
    def __init__(self, client: MetabaseClient, databases: dict[int, str]) -> None:
        self._client = client
        self._databases = databases
        self._lock = threading.Lock()
        self._loaded: set[int] = set()
        self._table_by_id: dict[int, TableRef] = {}
        self._table_id_by_ref: dict[TableRef, int] = {}
        self._field_by_id: dict[int, FieldRef] = {}
        self._field_id_by_ref: dict[FieldRef, int] = {}

    def table_ref(self, table_id: int) -> list[Any]:
        if table_id not in self._table_by_id:
            self._load_for(f"/api/table/{table_id}", f"table id {table_id}")
        if table_id not in self._table_by_id:
            raise UnresolvedReferenceError(f"table id {table_id} not found")
        return list(self._table_by_id[table_id])

    def table_id(self, ref: list[Any]) -> int:
        key = (ref[0], ref[1], ref[2])
        self._load(self._database_id(ref[0]))
        if key not in self._table_id_by_ref:
            raise UnresolvedReferenceError(f"table {list(key)} not found")
        return self._table_id_by_ref[key]

    def field_ref(self, field_id: int) -> list[Any]:
        if field_id not in self._field_by_id:
            self._load_for(f"/api/field/{field_id}", f"field id {field_id}")
        if field_id not in self._field_by_id:
            raise UnresolvedReferenceError(f"field id {field_id} not found")
        return list(self._field_by_id[field_id])

    def field_id(self, ref: list[Any]) -> int:
        key = tuple(ref)
        self._load(self._database_id(ref[0]))
        if key not in self._field_id_by_ref:
            raise UnresolvedReferenceError(f"field {list(key)} not found")
        return self._field_id_by_ref[key]

    def _database_id(self, name: str) -> int:
        for database_id, database in self._databases.items():
            if database == name:
                return database_id
        raise UnresolvedReferenceError(f"database {name!r} not found in Metabase")

    def _load_for(self, path: str, what: str) -> None:
        located = self._located(path, what)
        if located.db_id is None:
            located = located.table or self._located(
                f"/api/table/{located.table_id}", what
            )
        if located.db_id is None:
            raise UnresolvedReferenceError(f"{what} not found")
        self._load(located.db_id)

    def _located(self, path: str, what: str) -> Located:
        try:
            return Located.model_validate(self._client.get(path), extra="ignore")
        except ApiError as error:
            if error.status == 404:
                raise UnresolvedReferenceError(f"{what} not found") from error
            raise

    def _load(self, db_id: int) -> None:
        with self._lock:
            if db_id in self._loaded:
                return
            meta = DatabaseMeta.model_validate(
                self._client.get(
                    f"/api/database/{db_id}/metadata", include_hidden=True
                ),
                extra="ignore",
            )
            for table in meta.tables or []:
                table_ref: TableRef = (meta.name, table.schema_name, table.name)
                self._table_by_id[table.id] = table_ref
                self._table_id_by_ref[table_ref] = table.id
                fields = {f.id: f for f in table.fields or []}
                for field_id, found in fields.items():
                    path: list[str] = []
                    cursor: FieldMeta | None = found
                    while cursor is not None:
                        path.insert(0, cursor.name)
                        cursor = (
                            fields.get(cursor.parent_id) if cursor.parent_id else None
                        )
                    field_ref: FieldRef = (
                        meta.name,
                        table.schema_name,
                        table.name,
                        *path,
                    )
                    self._field_by_id[field_id] = field_ref
                    self._field_id_by_ref[field_ref] = field_id
            self._loaded.add(db_id)
