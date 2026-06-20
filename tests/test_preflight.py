"""Reference + database preflights both collect their problems before raising,
so a user with both a missing database AND a dangling card_path sees both
issues at once instead of having to fix-then-rerun-then-discover."""

from __future__ import annotations

from pathlib import Path

import pytest

from metabase_sync.apply import _database_preflight, _reference_preflight
from metabase_sync.diff import RemoteIndex
from metabase_sync.errors import PreflightError


def _seed_collections_and_dashboard(state_dir: Path, card_path_ref: str) -> Path:
    coll = state_dir / "collections" / "finance"
    (coll / "dashboards" / "review").mkdir(parents=True)
    (coll / "_collection.yaml").write_text("name: Finance\n")
    (coll / "dashboards" / "review" / "dashboard.yaml").write_text(
        "name: Review\n"
        "dashcards:\n"
        f"- card_path: {card_path_ref}\n"
        "  row: 0\n  col: 0\n  size_x: 12\n  size_y: 4\n"
    )
    return coll / "dashboards" / "review"


def test_reference_preflight_passes_with_valid_ref(tmp_path: Path):
    state = tmp_path / "state"
    review_dir = _seed_collections_and_dashboard(state, "../../cards/foo.sql")
    cards = review_dir.parent.parent / "cards"
    cards.mkdir()
    (cards / "foo.sql").write_text("---\nname: foo\n---\n---body---\nSELECT 1")

    assert _reference_preflight(state) == []


def test_reference_preflight_lists_dangling_card_path(tmp_path: Path):
    state = tmp_path / "state"
    _seed_collections_and_dashboard(state, "../../cards/missing.sql")

    problems = _reference_preflight(state)
    assert len(problems) == 1
    assert "missing.sql" in problems[0]
    assert "review/dashboard.yaml" in problems[0]


def test_reference_preflight_lists_dangling_pulse_dashboard_path(tmp_path: Path):
    state = tmp_path / "state"
    (state / "pulses").mkdir(parents=True)
    (state / "pulses" / "weekly.yaml").write_text(
        "name: weekly\n"
        "dashboard_path: ../collections/finance/dashboards/missing/dashboard.yaml\n"
        "cards: []\nchannels: []\n"
    )

    problems = _reference_preflight(state)
    assert len(problems) == 1
    assert "weekly.yaml" in problems[0]
    assert "missing/dashboard.yaml" in problems[0]


def test_database_preflight_lists_missing_and_mismatched(tmp_path: Path):
    state = tmp_path / "state"
    (state / "databases").mkdir(parents=True)
    (state / "databases" / "wanted.yaml").write_text(
        "name: wanted\nengine: postgres\nentity_id: null\n"
    )
    (state / "databases" / "drift.yaml").write_text(
        "name: drift\nengine: postgres\nentity_id: null\n"
    )

    remote = RemoteIndex()
    remote.databases_by_name["drift"] = {"name": "drift", "engine": "mysql", "id": 7}

    problems = _database_preflight(state, remote)
    assert any("wanted" in p and "missing" in p for p in problems)
    assert any("drift" in p and "mysql" in p for p in problems)


def test_run_collects_both_preflights(tmp_path: Path):
    """The crowning property: a user with both a missing database AND a
    dangling card_path sees both problems in one error."""
    from unittest.mock import MagicMock

    from metabase_sync.apply import run

    state = tmp_path / "state"
    _seed_collections_and_dashboard(state, "../../cards/missing.sql")
    (state / "databases").mkdir(exist_ok=True)
    (state / "databases" / "wanted.yaml").write_text(
        "name: wanted\nengine: postgres\nentity_id: null\n"
    )

    client = MagicMock()
    # Make build_remote_index return an empty index (no databases).
    import metabase_sync.apply as apply_module

    apply_module.build_remote_index = lambda c: RemoteIndex()  # type: ignore[assignment]

    with pytest.raises(PreflightError) as exc:
        run(client, state, mode="plan")
    msg = str(exc.value)
    assert "missing.sql" in msg
    assert "wanted" in msg and "missing" in msg
    assert "reference preflight" in msg
    assert "database preflight" in msg
