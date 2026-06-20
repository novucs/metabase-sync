"""`card_path` is the cross-reference format dashboards/pulses use. It's relative
to the *referring* file's directory. The resolver must handle both 'cards/x.sql'
(reference into the dashboard's own cards/ subdir) and '../../cards/x.sql'
(reference up into the collection's shared cards/)."""

from pathlib import Path

from metabase_sync.apply import _resolve_card_path


def test_resolve_card_path_in_same_dashboard_dir(tmp_path: Path):
    dashboard_dir = (
        tmp_path / "state" / "collections" / "finance" / "dashboards" / "review"
    )
    card = dashboard_dir / "cards" / "internal.sql"
    card.parent.mkdir(parents=True)
    card.touch()

    card_id_by_disk_path = {card.resolve(): 42}
    got = _resolve_card_path("cards/internal.sql", dashboard_dir, card_id_by_disk_path)
    assert got == 42


def test_resolve_card_path_walking_up_to_collection(tmp_path: Path):
    collection_dir = tmp_path / "state" / "collections" / "finance"
    dashboard_dir = collection_dir / "dashboards" / "review"
    card = collection_dir / "cards" / "shared.sql"
    card.parent.mkdir(parents=True)
    card.touch()
    dashboard_dir.mkdir(parents=True)

    card_id_by_disk_path = {card.resolve(): 100}
    got = _resolve_card_path(
        "../../cards/shared.sql", dashboard_dir, card_id_by_disk_path
    )
    assert got == 100


def test_resolve_card_path_unknown_returns_none(tmp_path: Path):
    dashboard_dir = (
        tmp_path / "state" / "collections" / "finance" / "dashboards" / "review"
    )
    dashboard_dir.mkdir(parents=True)
    got = _resolve_card_path("cards/does-not-exist.sql", dashboard_dir, {})
    assert got is None


def test_resolve_card_path_handles_null(tmp_path: Path):
    assert _resolve_card_path(None, tmp_path, {}) is None
    assert _resolve_card_path("", tmp_path, {}) is None
