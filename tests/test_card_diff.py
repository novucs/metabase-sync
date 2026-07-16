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


def test_visualization_settings_change_detected():
    desired = _desired("SELECT 1")
    remote = _remote_mbql5("SELECT 1")
    desired["visualization_settings"] = {
        "graph.metrics": ["total"],
        "series_settings": {"total": {"color": "#689636"}},
    }
    remote["visualization_settings"] = {"graph.metrics": ["total"]}

    diffs = _diff_card(desired, remote)

    assert [field for field, _b, _a in diffs] == ["visualization_settings"]
    assert "series_settings" in diffs[0][1]


def test_no_diff_when_visualization_settings_match():
    desired = _desired("SELECT 1")
    remote = _remote_mbql5("SELECT 1")
    settings = {"series_settings": {"total": {"color": "#689636"}}}
    desired["visualization_settings"] = dict(settings)
    remote["visualization_settings"] = dict(settings)
    assert _diff_card(desired, remote) == []


def test_no_diff_when_visualization_settings_empty_vs_missing():
    desired = _desired("SELECT 1")
    remote = _remote_mbql5("SELECT 1")
    desired["visualization_settings"] = {}
    assert _diff_card(desired, remote) == []


def test_scalar_field_changes_detected():
    desired = _desired("SELECT 1")
    remote = _remote_mbql5("SELECT 1")
    desired.update(
        type="model",
        database_id=2,
        enable_embedding=True,
        cache_ttl=300,
    )
    remote.update(
        type="question",
        database_id=1,
        enable_embedding=False,
        cache_ttl=None,
    )

    fields = {field for field, _b, _a in _diff_card(desired, remote)}

    assert {"type", "database_id", "enable_embedding", "cache_ttl"} <= fields


def test_parameters_and_embedding_params_changes_detected():
    desired = _desired("SELECT 1")
    remote = _remote_mbql5("SELECT 1")
    desired["parameters"] = [{"id": "p1", "type": "date/single"}]
    desired["embedding_params"] = {"p1": "enabled"}
    remote["parameters"] = []
    remote["embedding_params"] = None

    fields = {field for field, _b, _a in _diff_card(desired, remote)}

    assert {"parameters", "embedding_params"} <= fields


def test_empty_containers_and_missing_remote_type_do_not_diff():
    desired = _desired("SELECT 1")
    remote = _remote_mbql5("SELECT 1")
    desired["type"] = "question"
    desired["parameters"] = []
    desired["embedding_params"] = None
    remote["type"] = None  # older versions omit type; must not re-PUT forever
    remote["parameters"] = None
    remote["embedding_params"] = {}

    assert _diff_card(desired, remote) == []


def test_no_diff_when_sql_matches_across_forms():
    # Desired classic-native vs remote MBQL5-native with identical SQL must be a
    # no-op (idempotency: no spurious re-PUT).
    assert _diff_card(_desired("SELECT 1"), _remote_mbql5("SELECT 1")) == []


def test_live_dataset_query_wins_over_stale_legacy_query():
    # A native card whose live dataset_query already matches disk, but whose
    # legacy_query projection is stale, must be a no-op — the diff trusts the
    # live query, not the lagging legacy_query (else it re-PUTs every apply).
    desired = _desired("SELECT 1")
    remote = _remote_mbql5("SELECT 1")  # live query matches desired
    remote["legacy_query"] = (
        '{"database":1,"type":"native","native":{"query":"SELECT stale","template-tags":{}}}'
    )
    assert _diff_card(desired, remote) == []
