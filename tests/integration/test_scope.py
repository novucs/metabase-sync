"""`--collection` against a real server: export only the subtree, apply only
inside it, sticky scope, and first-apply creation of a new collection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from metabase_sync.cli import app
from metabase_sync.scope import SCOPE_FILE

from ._builders import unique_name, write_collection, write_native_card
from ._mb import MetabaseAdmin

pytestmark = pytest.mark.integration

runner = CliRunner()


def _env(url: str, key: str) -> dict[str, str]:
    return {"METABASE_URL": url, "METABASE_API_KEY": key, "CI": "true"}


def test_scoped_export_plan_apply(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    env = _env(metabase_url, metabase_api_key)
    a, b = unique_name("scope-a"), unique_name("scope-b")
    col_a = mb.create_collection(a)
    sub = mb.create_collection(unique_name("sub"), parent_id=col_a["id"])
    mb.create_native_card(unique_name("in-sub"), "SELECT 1", collection_id=sub["id"])
    col_b = mb.create_collection(b)
    other = mb.create_native_card(
        unique_name("other"), "SELECT 2", collection_id=col_b["id"]
    )

    state = tmp_path / "state"
    r = runner.invoke(app, ["export", "-s", str(state), "--collection", a], env=env)
    assert r.exit_code == 0, r.output
    tops = [d.name for d in (state / "collections").iterdir()]
    assert len(tops) == 1
    assert (state / SCOPE_FILE).read_text().strip() == col_a["entity_id"]
    assert not list(state.rglob("*other*"))

    # Sticky: no flag needed; clean plan.
    r = runner.invoke(app, ["plan", "-s", str(state)], env=env)
    assert r.exit_code == 0, r.output

    # New card applies inside A; B untouched.
    db = mb.sample_db_name()
    write_native_card(state, tops[0], "new", unique_name("new"), "SELECT 3", db)
    r = runner.invoke(app, ["apply", "-s", str(state), "--yes"], env=env)
    assert r.exit_code == 0, r.output
    assert mb.get_card(other["id"])["collection_id"] == col_b["id"]
    assert not mb.get_card(other["id"]).get("archived")

    # Rescoping the same state dir to another collection is refused.
    r = runner.invoke(app, ["plan", "-s", str(state), "--collection", b], env=env)
    assert r.exit_code != 0
    assert "does not match the scope saved" in (r.output + str(r.exception))


def test_first_apply_creates_and_pins_scope(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    env = _env(metabase_url, metabase_api_key)
    name = unique_name("scope-new")
    state = tmp_path / "state"
    coll_yaml = write_collection(state, name, name)

    r = runner.invoke(
        app, ["apply", "-s", str(state), "--yes", "--collection", name], env=env
    )
    assert r.exit_code == 0, r.output
    eid = yaml.safe_load(coll_yaml.read_text())["entity_id"]
    assert eid
    assert (state / SCOPE_FILE).read_text().strip() == eid

    r = runner.invoke(app, ["plan", "-s", str(state)], env=env)
    assert r.exit_code == 0, r.output
