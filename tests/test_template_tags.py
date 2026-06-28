"""Unit tests for native template-tag normalisation on apply.

A `dimension` (field-filter) template tag's field reference comes back from
Metabase in MBQL5 form; apply must convert it to classic form before sending it
inside a classic `type: native` query, and must never report success when a
write leaves the stored query empty.
"""

from __future__ import annotations

import pytest

from metabase_sync.apply._cards import (
    _assert_query_persisted,
    _build_dataset_query,
    _classic_field_ref,
    _normalize_template_tags,
)


def test_mbql5_field_ref_becomes_classic() -> None:
    ref = [
        "field",
        {
            "base-type": "type/DateTime",
            "lib/uuid": "x",
            "effective-type": "type/DateTime",
        },
        31,
    ]
    assert _classic_field_ref(ref) == ["field", 31, {"base-type": "type/DateTime"}]


def test_field_ref_with_only_lib_keys_collapses_to_null_opts() -> None:
    assert _classic_field_ref(["field", {"lib/uuid": "x"}, 7]) == ["field", 7, None]


def test_classic_field_ref_left_untouched() -> None:
    assert _classic_field_ref(["field", 31, None]) == ["field", 31, None]
    assert _classic_field_ref(["field", 31, {"base-type": "type/Integer"}]) == [
        "field",
        31,
        {"base-type": "type/Integer"},
    ]


def test_non_field_clause_untouched() -> None:
    assert _classic_field_ref(["expression", "foo"]) == ["expression", "foo"]
    assert _classic_field_ref("not-a-ref") == "not-a-ref"


def test_normalize_only_touches_dimension_tags() -> None:
    tags = {
        "date_range": {
            "type": "dimension",
            "name": "date_range",
            "dimension": ["field", {"lib/uuid": "x"}, 31],
        },
        "search": {"type": "text", "name": "search"},
    }
    out = _normalize_template_tags(tags)
    assert out["date_range"]["dimension"] == ["field", 31, None]
    assert out["search"] == {"type": "text", "name": "search"}  # untouched


def test_build_dataset_query_normalizes_native_tags() -> None:
    fm = {
        "template_tags": {
            "date_range": {
                "type": "dimension",
                "name": "date_range",
                "dimension": [
                    "field",
                    {"lib/uuid": "x", "base-type": "type/DateTime"},
                    31,
                ],
            }
        }
    }
    dq = _build_dataset_query(fm, "SELECT 1 WHERE {{date_range}}", None, 9)
    assert dq["native"]["template-tags"]["date_range"]["dimension"] == [
        "field",
        31,
        {"base-type": "type/DateTime"},
    ]


def test_guard_raises_when_query_silently_emptied() -> None:
    with pytest.raises(RuntimeError, match="empty dataset_query"):
        _assert_query_persisted(
            "card.sql", {"native": {"query": "SELECT 1"}}, {"dataset_query": {}}
        )


def test_guard_noop_when_nothing_queryable_sent() -> None:
    # Sending an empty query (e.g. a metadata-only card) must not trip the guard.
    _assert_query_persisted("card.sql", {}, {"dataset_query": {}})


def test_guard_passes_when_query_persisted() -> None:
    _assert_query_persisted(
        "card.sql",
        {"native": {"query": "SELECT 1"}},
        {"dataset_query": {"stages": [{"native": "SELECT 1"}]}},
    )
