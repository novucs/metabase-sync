"""Export-side integration tests: determinism, schema stamp, archived
exclusion, out-of-scope probing."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from metabase_sync.client import MetabaseClient
from metabase_sync.export import run_export
from metabase_sync.serialize.version import STATE_FORMAT_VERSION

from ._builders import unique_name
from ._mb import MetabaseAdmin

pytestmark = pytest.mark.integration


def _export(url: str, key: str, state: Path) -> None:
    with MetabaseClient(url, key) as client:
        run_export(client, state)


def test_export_against_empty(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Export produces a valid tree (collections + databases present)."""
    state = tmp_path / "state"
    _export(metabase_url, metabase_api_key, state)
    assert (state / "collections").exists()
    assert (state / "databases").exists()


def test_export_byte_stable(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Two consecutive exports are byte-identical."""
    a, b = tmp_path / "a", tmp_path / "b"
    _export(metabase_url, metabase_api_key, a)
    _export(metabase_url, metabase_api_key, b)
    a_files = {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()}
    b_files = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    assert a_files == b_files


def test_export_writes_schema_stamp(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    _export(metabase_url, metabase_api_key, state)
    stamp = state / ".metabase-sync-version"
    assert stamp.exists()
    assert stamp.read_text().strip() == str(STATE_FORMAT_VERSION)


def test_archived_card_excluded(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A card archived on the server must not appear in the export."""
    coll = mb.create_collection(unique_name("arch-coll"))
    name = unique_name("archived-card")
    card = mb.create_native_card(name, "SELECT 1", collection_id=coll["id"])
    mb.archive_card(card["id"])

    state = tmp_path / "state"
    _export(metabase_url, metabase_api_key, state)

    hits = [p for p in (state / "collections").rglob("*.sql") if name in p.read_text()]
    assert hits == [], f"archived card leaked into export: {hits}"


def test_out_of_scope_probe_is_version_robust(
    metabase_url: str, metabase_api_key: str
) -> None:
    """count_out_of_scope() must return a sane shape on a real server without
    raising, regardless of which of the alert/segment/metric endpoints exist on
    this Metabase version (they 404 on some)."""
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        counts = client.count_out_of_scope()
    assert set(counts) == {"alerts", "segments", "metrics"}
    assert all(isinstance(v, int) and v >= 0 for v in counts.values())


def test_out_of_scope_warning_fires_when_present(
    mb: MetabaseAdmin,
    metabase_url: str,
    metabase_api_key: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If an out-of-scope resource exists, export logs a warning. Segment
    creation is best-effort (the API shape varies by version); when it isn't
    possible we skip rather than fail."""
    tables = mb.get("/api/table")
    table = next((t for t in tables if t.get("db_id") == mb.sample_db_id()), None)
    if table is None:
        pytest.skip("no sample table to attach a segment to")
    try:
        mb.post(
            "/api/segment",
            {
                "name": unique_name("oos-segment"),
                "table_id": table["id"],
                "description": "",
                "definition": {
                    "source-table": table["id"],
                    "filter": ["!=", ["field", table["id"], None], 0],
                },
            },
        )
    except Exception:
        pytest.skip("segment creation not supported on this Metabase version")

    state = tmp_path / "state"
    with caplog.at_level(logging.WARNING, logger="metabase_sync"):
        _export(metabase_url, metabase_api_key, state)
    assert any("NOT yet synced" in r.message for r in caplog.records)
