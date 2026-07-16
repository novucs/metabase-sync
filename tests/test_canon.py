from __future__ import annotations

from metabase_sync.serialize import canon


def test_template_order_with_unknown_keys_after():
    doc = {"archived": False, "future_key": 1, "name": "c", "entity_id": "e"}
    out = canon.canonical(doc, canon.CARD)
    assert list(out) == ["entity_id", "name", "archived", "future_key"]


def test_nested_mappings_sorted():
    doc = {"name": "c", "visualization_settings": {"z": 1, "a": {"y": 2, "b": 3}}}
    out = canon.canonical(doc, canon.CARD)
    assert list(out["visualization_settings"]) == ["a", "z"]
    assert list(out["visualization_settings"]["a"]) == ["b", "y"]


def test_children_templates_apply_and_list_order_preserved():
    doc = {
        "name": "d",
        "dashcards": [
            {"size_y": 4, "row": 9, "card_path": "b.sql"},
            {"size_y": 6, "row": 0, "card_path": "a.sql"},
        ],
    }
    out = canon.canonical(doc, canon.DASHBOARD, canon.DASHBOARD_CHILDREN)
    assert [dc["card_path"] for dc in out["dashcards"]] == ["b.sql", "a.sql"]
    assert list(out["dashcards"][0]) == ["card_path", "row", "size_y"]


def test_idempotent():
    doc = {
        "name": "d",
        "tabs": [{"position": 0, "name": "t"}],
        "extra": {"b": 1, "a": 2},
    }
    once = canon.canonical(doc, canon.DASHBOARD, canon.DASHBOARD_CHILDREN)
    assert canon.canonical(once, canon.DASHBOARD, canon.DASHBOARD_CHILDREN) == once
