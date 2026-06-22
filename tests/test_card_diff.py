"""Regression guard for the diff path.

On Metabase >= v0.62, a card created/updated via the API returns
`legacy_query = null` and the query only lives in `dataset_query` (MBQL5). The
diff must fall back to `dataset_query` in that case — otherwise SQL edits go
undetected and silently never apply (a real bug an integration test caught).
"""

from __future__ import annotations

from typing import Any

from metabase_sync.apply._cards import _diff_card


def _desired(sql: str) -> dict[str, Any]:
    return {
        "name": "c",
        "description": None,
        "display": "scalar",
        "collection_id": None,
        "archived": False,
        "dataset_query": {
            "database": 1,
            "type": "native",
            "native": {"query": sql, "template-tags": {}},
        },
    }


def _remote_mbql5(sql: str) -> dict[str, Any]:
    """A remote card as v0.62 returns it: legacy_query null, query in MBQL5."""
    return {
        "name": "c",
        "description": None,
        "display": "scalar",
        "collection_id": None,
        "archived": False,
        "legacy_query": None,
        "dataset_query": {
            "lib/type": "mbql/query",
            "database": 1,
            "stages": [{"lib/type": "mbql.stage/native", "native": sql}],
        },
    }


def test_sql_change_detected_when_legacy_query_null():
    diffs = _diff_card(_desired("SELECT 2"), _remote_mbql5("SELECT 1"))
    assert any(field == "SQL" for field, _b, _a in diffs)


def test_no_diff_when_sql_matches_across_forms():
    # Desired classic-native vs remote MBQL5-native with identical SQL must be a
    # no-op (idempotency: no spurious re-PUT).
    assert _diff_card(_desired("SELECT 1"), _remote_mbql5("SELECT 1")) == []


def test_legacy_query_still_preferred_when_present():
    desired = _desired("SELECT 2")
    remote = _remote_mbql5("SELECT 1")
    remote["legacy_query"] = (
        '{"database":1,"type":"native","native":{"query":"SELECT 1","template-tags":{}}}'
    )
    diffs = _diff_card(desired, remote)
    assert any(field == "SQL" for field, _b, _a in diffs)
