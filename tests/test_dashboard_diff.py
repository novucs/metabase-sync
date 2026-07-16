"""The dashboard diff must cover every field the PUT sends: metadata that only
plan-skipped before (width, parameters, embedding, position) and dashcard
content beyond geometry (tab moves, settings, mappings, series)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from metabase_sync.apply._dashboards import _contents_diff, _metadata_diffs

DIR = Path("/tmp/does-not-matter")


def _desired_meta(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "d",
        "description": None,
        "collection_id": 1,
        "parameters": [],
        "auto_apply_filters": True,
        "cache_ttl": None,
        "enable_embedding": False,
        "embedding_params": None,
        "width": "fixed",
        "position": None,
        "archived": False,
    }
    base.update(overrides)
    return base


def _dashcard(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "card_path": None,
        "tab_position": None,
        "row": 0,
        "col": 0,
        "size_x": 8,
        "size_y": 4,
        "parameter_mappings": [],
        "visualization_settings": {},
        "series": [],
    }
    base.update(overrides)
    return base


def _remote_dashcard(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "card_id": None,
        "dashboard_tab_id": None,
        "row": 0,
        "col": 0,
        "size_x": 8,
        "size_y": 4,
        "parameter_mappings": None,
        "visualization_settings": None,
        "series": [],
    }
    base.update(overrides)
    return base


def test_metadata_gaps_detected():
    desired = _desired_meta(
        width="full",
        parameters=[{"id": "p1"}],
        enable_embedding=True,
        cache_ttl=60,
        position=2,
    )
    remote = _desired_meta()

    fields = {f for f, _b, _a in _metadata_diffs(desired, remote)}

    assert {
        "width",
        "parameters",
        "enable_embedding",
        "cache_ttl",
        "position",
    } <= fields


def test_metadata_empty_containers_equal():
    desired = _desired_meta(parameters=[], embedding_params=None)
    remote = _desired_meta(parameters=None, embedding_params={})
    assert _metadata_diffs(desired, remote) == []


def test_dashcard_tab_move_detected():
    doc = {
        "tabs": [{"name": "a", "position": 0}, {"name": "b", "position": 1}],
        "dashcards": [_dashcard(tab_position=1)],
    }
    remote = {
        "tabs": [
            {"id": 10, "name": "a", "position": 0},
            {"id": 11, "name": "b", "position": 1},
        ],
        "dashcards": [_remote_dashcard(dashboard_tab_id=10)],
    }
    assert "dashcards" in _contents_diff(doc, remote, DIR, {})


def test_dashcard_settings_and_mappings_detected():
    doc = {
        "tabs": [],
        "dashcards": [
            _dashcard(
                visualization_settings={"graph.metrics": ["total"]},
                parameter_mappings=[{"parameter_id": "p1"}],
            )
        ],
    }
    remote = {"tabs": [], "dashcards": [_remote_dashcard()]}
    assert "dashcards" in _contents_diff(doc, remote, DIR, {})


def test_dashcard_series_compared_by_card_id():
    doc = {
        "tabs": [],
        "dashcards": [_dashcard(series=[{"id": 5, "name": "on disk"}])],
    }
    remote = {
        "tabs": [],
        "dashcards": [
            _remote_dashcard(series=[{"id": 5, "name": "full remote card summary"}])
        ],
    }
    assert _contents_diff(doc, remote, DIR, {}) == ""


def test_identical_dashcards_no_diff():
    doc = {
        "tabs": [{"name": "a", "position": 0}],
        "dashcards": [_dashcard(tab_position=0)],
    }
    remote = {
        "tabs": [{"id": 10, "name": "a", "position": 0}],
        "dashcards": [_remote_dashcard(dashboard_tab_id=10)],
    }
    assert _contents_diff(doc, remote, DIR, {}) == ""
