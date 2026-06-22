"""`MetabaseAdmin` — a thin httpx wrapper over the Metabase REST API for test
setup. Kills the per-call `httpx.get(f"{url}/api/...", headers=..., timeout=10)`
boilerplate and names the server-side fixtures (create card, dashboard, pulse…)
so tests read as intent, not transport.
"""

from __future__ import annotations

from typing import Any

import httpx


class MetabaseAdmin:
    def __init__(self, url: str, api_key: str, timeout: float = 30.0) -> None:
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # --- raw verbs ----------------------------------------------------------

    def get(self, path: str) -> Any:
        r = self._http.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json: dict[str, Any]) -> Any:
        r = self._http.post(path, json=json)
        r.raise_for_status()
        return r.json()

    def put(self, path: str, json: dict[str, Any]) -> Any:
        r = self._http.put(path, json=json)
        r.raise_for_status()
        return r.json()

    # --- databases ----------------------------------------------------------

    def sample_db_id(self) -> int:
        dbs = self.get("/api/database")["data"]
        db = next(d for d in dbs if d["name"] != "Internal Metabase Database")
        return int(db["id"])

    def sample_db_name(self) -> str:
        dbs = self.get("/api/database")["data"]
        db = next(d for d in dbs if d["name"] != "Internal Metabase Database")
        return str(db["name"])

    def sample_table_id(self) -> int:
        """A table id on the sample DB, for authoring GUI/MBQL queries."""
        db_id = self.sample_db_id()
        tables = self.get("/api/table")
        return int(next(t for t in tables if t.get("db_id") == db_id)["id"])

    # --- collections --------------------------------------------------------

    def create_collection(
        self, name: str, parent_id: int | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "color": "#509EE3"}
        if parent_id is not None:
            body["parent_id"] = parent_id
        return self.post("/api/collection", body)

    # --- cards --------------------------------------------------------------

    def create_native_card(
        self,
        name: str,
        sql: str,
        *,
        db_id: int | None = None,
        collection_id: int | None = None,
        card_type: str = "question",
        display: str = "scalar",
        template_tags: dict[str, Any] | None = None,
        result_metadata: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        db_id = db_id if db_id is not None else self.sample_db_id()
        return self.post(
            "/api/card",
            {
                "name": name,
                "type": card_type,
                "display": display,
                "database_id": db_id,
                "dataset_query": {
                    "database": db_id,
                    "type": "native",
                    "native": {"query": sql, "template-tags": template_tags or {}},
                },
                "visualization_settings": {},
                "parameters": [],
                "collection_id": collection_id,
                "result_metadata": result_metadata or [],
            },
        )

    def create_model(
        self, name: str, sql: str, *, collection_id: int | None = None, **kw: Any
    ) -> dict[str, Any]:
        return self.create_native_card(
            name,
            sql,
            card_type="model",
            display="table",
            collection_id=collection_id,
            **kw,
        )

    def get_card(self, card_id: int) -> dict[str, Any]:
        return self.get(f"/api/card/{card_id}")

    def find_card(self, name: str) -> dict[str, Any]:
        """Fetch full detail of the (non-archived) card with this name."""
        summary = next(
            c
            for c in self.get("/api/card")
            if c["name"] == name and not c.get("archived")
        )
        return self.get_card(summary["id"])

    def update_card(self, card_id: int, body: dict[str, Any]) -> dict[str, Any]:
        return self.put(f"/api/card/{card_id}", body)

    def archive_card(self, card_id: int) -> dict[str, Any]:
        return self.put(f"/api/card/{card_id}", {"archived": True})

    def run_card_query(self, card_id: int) -> None:
        # Populates result_metadata server-side. Ignore the result body.
        self._http.post(f"/api/card/{card_id}/query", json={}).raise_for_status()

    # --- dashboards ---------------------------------------------------------

    def create_dashboard(
        self, name: str, collection_id: int | None = None
    ) -> dict[str, Any]:
        return self.post(
            "/api/dashboard", {"name": name, "collection_id": collection_id}
        )

    def set_dashboard_contents(
        self,
        dashboard_id: int,
        dashcards: list[dict[str, Any]],
        *,
        tabs: list[dict[str, Any]] | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"dashcards": dashcards, "tabs": tabs or []}
        if name is not None:
            body["name"] = name
        return self.put(f"/api/dashboard/{dashboard_id}", body)

    def get_dashboard(self, dashboard_id: int) -> dict[str, Any]:
        return self.get(f"/api/dashboard/{dashboard_id}")

    def list_dashboards(self) -> list[dict[str, Any]]:
        return self.get("/api/dashboard")

    def find_dashboard(self, name: str) -> dict[str, Any]:
        return next(d for d in self.list_dashboards() if d["name"] == name)

    # --- pulses -------------------------------------------------------------

    def create_pulse(
        self,
        name: str,
        *,
        dashboard_id: int,
        collection_id: int | None,
        cards: list[dict[str, Any]],
        channels: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.post(
            "/api/pulse",
            {
                "name": name,
                "dashboard_id": dashboard_id,
                "collection_id": collection_id,
                "cards": cards,
                "channels": channels,
                "skip_if_empty": False,
                "parameters": [],
            },
        )

    # --- users --------------------------------------------------------------

    def admin_user(self) -> dict[str, Any]:
        users = self.get("/api/user")
        data = users["data"] if isinstance(users, dict) else users
        return next(u for u in data if u.get("is_superuser"))
