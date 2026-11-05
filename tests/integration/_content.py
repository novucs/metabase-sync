import uuid
from dataclasses import dataclass
from typing import Any

from ._mb import MetabaseAdmin


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


@dataclass
class Rich:
    collection: dict[str, Any]
    child: dict[str, Any]
    snippet: dict[str, Any]
    native: dict[str, Any]
    derived: dict[str, Any]
    gui: dict[str, Any]
    model: dict[str, Any]
    dashboard: dict[str, Any]
    question: dict[str, Any]
    pulse: dict[str, Any]


def _field(mb: MetabaseAdmin, table: str, field: str) -> int:
    meta = mb.get(f"/api/database/{mb.sample_db_id()}/metadata")
    t = next(t for t in meta["tables"] if t["name"] == table)
    return next(f["id"] for f in t["fields"] if f["name"] == field)


def _table(mb: MetabaseAdmin, table: str) -> int:
    meta = mb.get(f"/api/database/{mb.sample_db_id()}/metadata")
    return next(t["id"] for t in meta["tables"] if t["name"] == table)


def build_rich(mb: MetabaseAdmin) -> Rich:
    db = mb.sample_db_id()
    created_at = _field(mb, "ORDERS", "CREATED_AT")
    collection = mb.create_collection(unique("Rich"))
    child = mb.create_collection("Child", parent_id=collection["id"])
    mb.put(f"/api/collection/{collection['id']}", {"description": "Everything"})
    mb.put("/api/setting/enable-embedding-static", {"value": True})
    snippet = mb.post(
        "/api/native-query-snippet",
        {
            "name": unique("only real orders"),
            "content": "TOTAL > 0",
            "description": "Excludes refunds",
        },
    )
    created_tag = str(uuid.uuid4())
    native = mb.post(
        "/api/card",
        {
            "name": "Orders by date",
            "display": "scalar",
            "collection_id": collection["id"],
            "visualization_settings": {},
            "parameters": [
                {
                    "id": created_tag,
                    "name": "Created",
                    "slug": "created",
                    "type": "date/all-options",
                    "target": ["dimension", ["template-tag", "created"]],
                }
            ],
            "dataset_query": {
                "database": db,
                "type": "native",
                "native": {
                    "query": "SELECT count(*)\nFROM ORDERS\nWHERE {{created}} AND {{snippet: "
                    + snippet["name"]
                    + "}}",
                    "template-tags": {
                        "created": {
                            "id": created_tag,
                            "name": "created",
                            "display-name": "Created",
                            "type": "dimension",
                            "dimension": ["field", created_at, None],
                            "widget-type": "date/all-options",
                        },
                        f"snippet: {snippet['name']}": {
                            "id": str(uuid.uuid4()),
                            "name": f"snippet: {snippet['name']}",
                            "display-name": "Snippet",
                            "type": "snippet",
                            "snippet-name": snippet["name"],
                            "snippet-id": snippet["id"],
                        },
                    },
                },
            },
        },
    )
    tag = f"#{native['id']}-orders-by-date"
    derived = mb.post(
        "/api/card",
        {
            "name": "Derived",
            "display": "table",
            "collection_id": collection["id"],
            "visualization_settings": {},
            "dataset_query": {
                "database": db,
                "type": "native",
                "native": {
                    "query": "SELECT * FROM {{" + tag + "}}",
                    "template-tags": {
                        tag: {
                            "id": str(uuid.uuid4()),
                            "name": tag,
                            "display-name": "Orders by date",
                            "type": "card",
                            "card-id": native["id"],
                        }
                    },
                },
            },
        },
    )
    gui = mb.post(
        "/api/card",
        {
            "name": "Orders per month",
            "display": "line",
            "collection_id": child["id"],
            "visualization_settings": {
                "graph.dimensions": ["CREATED_AT"],
                "graph.metrics": ["count"],
            },
            "dataset_query": {
                "database": db,
                "type": "query",
                "query": {
                    "source-table": _table(mb, "ORDERS"),
                    "aggregation": [["count"]],
                    "breakout": [["field", created_at, {"temporal-unit": "month"}]],
                },
            },
        },
    )
    mb.put(
        f"/api/card/{native['id']}",
        {
            "description": "Counts real orders",
            "parameter_mappings": [
                {
                    "parameter_id": created_tag,
                    "card_id": gui["id"],
                    "target": ["dimension", ["field", created_at, None]],
                }
            ],
            "collection_position": 1,
            "collection_preview": False,
            "enable_embedding": True,
            "embedding_type": "guest-embed",
            "embedding_params": {"created": "enabled"},
        },
    )
    for source, target in ((native, derived), (derived, native)):
        mb.put(
            f"/api/card/{source['id']}",
            {
                "visualization_settings": {
                    "click_behavior": {
                        "type": "link",
                        "linkType": "question",
                        "targetId": target["id"],
                        "parameterMapping": {},
                    }
                }
            },
        )
    mb.put(
        f"/api/card/{gui['id']}",
        {
            "visualization_settings": {
                "graph.dimensions": ["CREATED_AT"],
                "graph.metrics": ["count"],
                "column_settings": {
                    '["name","count"]': {
                        "click_behavior": {
                            "type": "link",
                            "linkType": "question",
                            "targetId": gui["id"],
                            "parameterMapping": {},
                        }
                    }
                },
            }
        },
    )
    model = mb.create_model(
        "Orders model",
        "SELECT ID, PRODUCT_ID FROM ORDERS",
        collection_id=collection["id"],
        result_metadata=[
            {
                "name": "ID",
                "display_name": "Order",
                "description": "The order's id",
                "base_type": "type/BigInteger",
                "effective_type": "type/BigInteger",
                "semantic_type": "type/PK",
                "field_ref": ["field", "ID", {"base-type": "type/BigInteger"}],
                "settings": {"column_title": "Order number"},
                "visibility_type": "details-only",
            },
            {
                "name": "PRODUCT_ID",
                "display_name": "Product",
                "base_type": "type/Integer",
                "semantic_type": "type/FK",
                "fk_target_field_id": _field(mb, "PRODUCTS", "ID"),
                "field_ref": ["field", "PRODUCT_ID", {"base-type": "type/Integer"}],
            },
        ],
    )
    dashboard = mb.post(
        "/api/dashboard", {"name": "Overview", "collection_id": collection["id"]}
    )
    mb.put(
        f"/api/dashboard/{dashboard['id']}",
        {
            "description": "The overview",
            "collection_position": 2,
            "width": "full",
            "auto_apply_filters": False,
            "enable_embedding": True,
            "embedding_type": "guest-embed",
            "embedding_params": {"created": "enabled"},
        },
    )
    question = mb.post(
        "/api/card",
        {
            "name": "Dashboard question",
            "display": "scalar",
            "collection_id": collection["id"],
            "dashboard_id": dashboard["id"],
            "visualization_settings": {},
            "dataset_query": {
                "database": db,
                "type": "native",
                "native": {"query": "SELECT 1", "template-tags": {}},
            },
        },
    )
    current = mb.get(f"/api/dashboard/{dashboard['id']}")
    auto = [
        {k: dc[k] for k in ("id", "card_id", "row", "col", "size_x", "size_y")}
        for dc in current.get("dashcards") or []
    ]
    mb.put(
        f"/api/dashboard/{dashboard['id']}",
        {
            "parameters": [
                {
                    "id": "d1",
                    "name": "Created",
                    "slug": "created",
                    "type": "date/all-options",
                }
            ],
            "tabs": [
                {"id": -1, "name": "Main", "position": 0},
                {"id": -2, "name": "Notes", "position": 1},
            ],
            "dashcards": [
                {
                    "id": -1,
                    "card_id": native["id"],
                    "dashboard_tab_id": -1,
                    "row": 0,
                    "col": 0,
                    "size_x": 6,
                    "size_y": 4,
                    "inline_parameters": ["d1"],
                    "parameter_mappings": [
                        {
                            "parameter_id": "d1",
                            "card_id": native["id"],
                            "target": ["dimension", ["template-tag", "created"]],
                        }
                    ],
                },
                {
                    "id": -2,
                    "card_id": gui["id"],
                    "dashboard_tab_id": -1,
                    "row": 0,
                    "col": 6,
                    "size_x": 12,
                    "size_y": 6,
                    "series": [{"id": derived["id"]}],
                    "visualization_settings": {
                        "click_behavior": {
                            "type": "link",
                            "linkType": "question",
                            "targetId": derived["id"],
                            "parameterMapping": {},
                        }
                    },
                },
                {
                    "id": -3,
                    "card_id": None,
                    "dashboard_tab_id": -2,
                    "row": 0,
                    "col": 0,
                    "size_x": 12,
                    "size_y": 3,
                    "visualization_settings": {
                        "virtual_card": {
                            "name": None,
                            "display": "text",
                            "visualization_settings": {},
                            "archived": False,
                        },
                        "text": "Read me",
                    },
                },
                {
                    "id": -4,
                    "card_id": None,
                    "dashboard_tab_id": -2,
                    "row": 10,
                    "col": 0,
                    "size_x": 6,
                    "size_y": 2,
                    "visualization_settings": {
                        "virtual_card": {
                            "name": None,
                            "display": "link",
                            "visualization_settings": {},
                            "archived": False,
                        },
                        "link": {
                            "entity": {
                                "id": derived["id"],
                                "model": "card",
                                "name": "Derived",
                                "display": "table",
                            }
                        },
                    },
                },
                *[{**a, "dashboard_tab_id": -2, "row": 3} for a in auto],
            ],
        },
    )
    if not auto:
        detail = mb.get(f"/api/dashboard/{dashboard['id']}")
        tabs = detail["tabs"]
        notes = next(t["id"] for t in tabs if t["name"] == "Notes")
        dashcards = [
            {
                k: dc[k]
                for k in (
                    "id",
                    "card_id",
                    "dashboard_tab_id",
                    "row",
                    "col",
                    "size_x",
                    "size_y",
                    "parameter_mappings",
                    "visualization_settings",
                )
            }
            for dc in detail["dashcards"]
        ]
        dashcards.append(
            {
                "id": -9,
                "card_id": question["id"],
                "dashboard_tab_id": notes,
                "row": 3,
                "col": 0,
                "size_x": 4,
                "size_y": 3,
            }
        )
        mb.put(
            f"/api/dashboard/{dashboard['id']}",
            {
                "tabs": [
                    {"id": t["id"], "name": t["name"], "position": t["position"]}
                    for t in tabs
                ],
                "dashcards": dashcards,
            },
        )
    detail = mb.get(f"/api/dashboard/{dashboard['id']}")
    first = next(dc for dc in detail["dashcards"] if dc["card_id"] == native["id"])
    admin = mb.admin_user()
    pulse = mb.post(
        "/api/pulse",
        {
            "name": "Daily overview",
            "dashboard_id": dashboard["id"],
            "cards": [
                {
                    "id": native["id"],
                    "include_csv": True,
                    "include_xls": True,
                    "format_rows": False,
                    "pivot_results": True,
                    "dashboard_card_id": first["id"],
                }
            ],
            "channels": [
                {
                    "channel_type": "email",
                    "enabled": False,
                    "schedule_type": "monthly",
                    "schedule_hour": 8,
                    "schedule_day": "mon",
                    "schedule_frame": "first",
                    "recipients": [{"id": admin["id"]}],
                }
            ],
            "skip_if_empty": True,
            "parameters": [
                {
                    "id": "d1",
                    "name": "Created",
                    "slug": "created",
                    "type": "date/all-options",
                    "value": "past30days",
                }
            ],
        },
    )
    return Rich(
        collection, child, snippet, native, derived, gui, model, detail, question, pulse
    )
