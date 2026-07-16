"""The load-bearing apply guarantees, proven against a real server:
idempotency + entity_id write-back, optimistic-concurrency drift, both
preflights, and the schema-version gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metabase_sync.apply import run as run_apply
from metabase_sync.client import MetabaseClient
from metabase_sync.errors import ConcurrencyDriftError, PreflightError
from metabase_sync.export import run_export
from metabase_sync.serialize.version import StateFormatError
from metabase_sync.serialize.yamlio import (
    read_frontmatter_sql,
    write_frontmatter_sql,
    write_yaml,
)

from ._builders import (
    dashcard_to,
    unique_name,
    write_collection,
    write_dashboard,
    write_native_card,
)
from ._mb import MetabaseAdmin

pytestmark = pytest.mark.integration


def _apply(url: str, key: str, state: Path, **kw):
    with MetabaseClient(url, key) as client:
        return run_apply(client, state, **kw)


def test_apply_is_idempotent_with_writeback(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """The headline promise: a second apply is a pure no-op. Proves both that
    entity_id is written back to disk on create AND that the write-back makes
    re-apply match by entity_id (no duplicates)."""
    db = mb.sample_db_name()
    state = tmp_path / "state"
    coll_yaml = write_collection(state, "idem", unique_name("idem"))
    card_sql = write_native_card(
        state, "idem", "c", unique_name("idem-card"), "SELECT 1", db
    )
    dash_yaml = write_dashboard(
        state, "idem", "d", unique_name("idem-dash"), dashcards=[dashcard_to("c.sql")]
    )

    first = _apply(metabase_url, metabase_api_key, state, mode="apply")
    assert sum(1 for c in first.changes if c.action == "create") == 3

    # Write-back fired: entity_ids are now real on disk.
    assert yaml.safe_load(coll_yaml.read_text())["entity_id"]
    assert yaml.safe_load(dash_yaml.read_text())["entity_id"]
    assert read_frontmatter_sql(card_sql)[0]["entity_id"]

    # Second apply: everything skips. No duplicates created.
    second = _apply(metabase_url, metabase_api_key, state, mode="apply")
    assert all(c.action == "skip" for c in second.changes), (
        f"re-apply was not a no-op: {[(c.resource, c.action) for c in second.changes if c.action != 'skip']}"
    )


def test_settings_only_edit_plans_update_and_settles(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A disk edit touching only visualization_settings used to plan as a skip
    forever; it must plan as one update, apply, and then settle."""
    db = mb.sample_db_name()
    state = tmp_path / "state"
    write_collection(state, "vs", unique_name("vs"))
    card_sql = write_native_card(
        state, "vs", "c", unique_name("vs-card"), "SELECT 1", db
    )
    _apply(metabase_url, metabase_api_key, state, mode="apply")

    fm, body = read_frontmatter_sql(card_sql)
    fm["visualization_settings"] = {"series_settings": {"total": {"color": "#689636"}}}
    write_frontmatter_sql(card_sql, fm, body)

    plan = _apply(metabase_url, metabase_api_key, state, mode="plan")
    updates = [c for c in plan.changes if c.action == "update"]
    assert [c.resource for c in updates] == ["cards"]
    assert "visualization_settings" in updates[0].summary

    _apply(metabase_url, metabase_api_key, state, mode="apply")
    settled = _apply(metabase_url, metabase_api_key, state, mode="plan")
    assert all(c.action == "skip" for c in settled.changes)


def test_plan_no_op_after_export(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
        plan = run_apply(client, state, mode="plan")
    for resource, c in plan.counts().items():
        assert c["create"] == 0 and c["update"] == 0 and c["archive"] == 0, (
            f"{resource}: {c}"
        )


def test_only_flag_restricts_resource(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
        plan = run_apply(client, state, mode="plan", only={"cards"})
    assert {c.resource for c in plan.changes} <= {"cards"}


def test_concurrency_drift_aborts_without_force(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """plan → UI edit → apply must refuse (the snapshot's updated_at no longer
    matches), and --force must override."""
    db = mb.sample_db_name()
    name = unique_name("drift-card")
    state = tmp_path / "state"
    write_collection(state, "drift", unique_name("drift"))
    write_native_card(state, "drift", "c", name, "SELECT 1", db)
    _apply(metabase_url, metabase_api_key, state, mode="apply")

    with MetabaseClient(metabase_url, metabase_api_key) as client:
        preview = run_apply(client, state, mode="plan")

    # Someone edits the card in the UI between plan and apply.
    server_card = mb.find_card(name)
    mb.update_card(server_card["id"], {"name": name + "-edited"})

    with MetabaseClient(metabase_url, metabase_api_key) as client:
        with pytest.raises(ConcurrencyDriftError):
            run_apply(
                client,
                state,
                mode="apply",
                concurrency_snapshot=preview.concurrency_snapshot,
            )
        # --force overrides.
        run_apply(
            client,
            state,
            mode="apply",
            force=True,
            concurrency_snapshot=preview.concurrency_snapshot,
        )


def test_database_preflight_aborts(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A databases/ manifest naming a non-existent DB aborts before any write."""
    state = tmp_path / "state"
    coll_name = unique_name("dbpf")
    write_collection(state, "dbpf", coll_name)
    (state / "databases").mkdir(parents=True)
    write_yaml(
        state / "databases" / "ghost.yaml",
        {"name": "no-such-db-xyz", "engine": "h2", "entity_id": None},
    )

    with pytest.raises(PreflightError):
        _apply(metabase_url, metabase_api_key, state, mode="apply")

    # Nothing was created.
    assert not any(c.get("name") == coll_name for c in mb.get("/api/collection"))


def test_reference_preflight_aborts(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A dashcard card_path pointing at a missing file aborts before any HTTP write."""
    state = tmp_path / "state"
    write_collection(state, "refpf", unique_name("refpf"))
    write_dashboard(
        state,
        "refpf",
        "d",
        unique_name("refpf-dash"),
        dashcards=[dashcard_to("ghost.sql")],
    )
    with pytest.raises(PreflightError):
        _apply(metabase_url, metabase_api_key, state, mode="apply")


def test_newer_schema_stamp_blocks_apply(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """A state tree stamped by a newer tool version must be refused."""
    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)
    (state / ".metabase-sync-version").write_text("999\n")
    with pytest.raises(StateFormatError):
        _apply(metabase_url, metabase_api_key, state, mode="plan")
