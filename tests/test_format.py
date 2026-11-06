import contextlib
import os
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from metabase_sync.cli import app
from metabase_sync.errors import ConfigError, StateError, ValidationFailed
from metabase_sync.format import representations
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    SUBSCRIPTION,
)
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.collection import Collection
from metabase_sync.format.representations.content import UUID_NAMESPACE, stable_uuids
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.format.representations.subscription import Channel, Subscription
from metabase_sync.store import tree, yamlio
from metabase_sync.store.journal import Created, Journal, Started
from metabase_sync.store.tree import Entity
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.apply import rename_ids


def collection_file(root: Path, name: str, entity_id: str | None = "C" * 21) -> Path:
    yamlio.write(root / tree.CONFIG, {"removal": "archive"})
    path = root / "collections" / "main" / f"{name.lower()}.yaml"
    data = {"name": name, "serdes/meta": [{"model": "Collection"}]}
    yamlio.write(path, {**data, "entity_id": entity_id} if entity_id else data)
    return path


def test_commands_outside_a_configured_directory_say_so(tmp_path):
    with pytest.raises(ConfigError) as raised:
        workflow.fmt(tmp_path)

    assert f"has no {tree.CONFIG}" in str(raised.value)


def test_files_without_ids_get_stable_provisional_ids(tmp_path):
    collection_file(tmp_path, "Sales", entity_id=None)

    first, second = tree.read_tree(tmp_path), tree.read_tree(tmp_path)

    assert not first.problems
    assert first.entities.keys() == second.entities.keys()
    assert all(e.provisional for e in first.entities.values())


def test_fmt_waits_for_an_interrupted_apply_to_finish(tmp_path):
    path = collection_file(tmp_path, "Sales", entity_id=None)
    Journal(tmp_path).record(
        Started(
            op="a:base:create",
            action="create",
            kind="collection",
            entity_id="A" * 21,
            name="Sales",
        )
    )

    with pytest.raises(StateError):
        workflow.fmt(tmp_path)

    assert "entity_id" not in yamlio.read(path)


def test_a_truncated_journal_line_does_not_hide_the_entries_after_it(tmp_path):
    journal = Journal(tmp_path)
    created = Created(
        op="a:base:create",
        kind="card",
        entity_id="A" * 21,
        created_id=5,
        created_entity_id="B" * 21,
    )
    journal.record(created)
    journal.path.write_text('{"status": "pos\n' + journal.path.read_text())

    assert journal.entries() == [created]


def test_recording_new_ids_keeps_the_comments_in_the_config(tmp_path):
    config = tmp_path / tree.CONFIG
    config.write_text(f"# sales team\ncollections:\n  - {'A' * 21}\n")

    rename_ids(tmp_path, {"A" * 21: "B" * 21})

    assert config.read_text() == f"# sales team\ncollections:\n  - {'B' * 21}\n"


def test_recording_new_ids_reports_invalid_yaml_by_file_and_line(tmp_path):
    broken = tmp_path / "collections" / "main" / "broken.yaml"
    broken.parent.mkdir(parents=True)
    broken.write_text("name: x\nentity_id: [\n")

    with pytest.raises(ConfigError) as raised:
        rename_ids(tmp_path, {"A" * 21: "B" * 21})

    assert str(raised.value).startswith(
        "collections/main/broken.yaml: invalid YAML at line 3"
    )


def test_fmt_reports_invalid_yaml_by_file(tmp_path):
    collection_file(tmp_path, "Sales")
    (tmp_path / "collections" / "main" / "broken.yaml").write_text("name: [")

    with pytest.raises(ValidationFailed) as raised:
        workflow.fmt(tmp_path)

    assert any("broken.yaml: invalid YAML" in p for p in raised.value.problems)


def test_a_move_interrupted_before_cleanup_leaves_no_duplicate(tmp_path, monkeypatch):
    collection_file(tmp_path, "Sales").rename(
        tmp_path / "collections" / "main" / "misplaced.yaml"
    )

    def interrupted(self, missing_ok=False):
        raise OSError("interrupted")

    monkeypatch.setattr(Path, "unlink", interrupted)
    with contextlib.suppress(OSError):
        workflow.fmt(tmp_path)
    monkeypatch.undo()

    local = tree.read_tree(tmp_path)
    assert not local.problems
    assert [e.path for e in local.entities.values()] == [
        Path("collections/main/sales.yaml")
    ]


def test_names_with_square_brackets_are_printed_as_written(tmp_path):
    collection_file(tmp_path, "a", entity_id="A" * 21)
    collection_file(tmp_path, "b", entity_id="B" * 21)
    for path in (tmp_path / "collections" / "main").glob("*.yaml"):
        yamlio.write(path, {**yamlio.read(path), "name": "Rev [/draft]"})

    result = CliRunner().invoke(app, ["validate", "--chdir", str(tmp_path)])

    assert result.exit_code == 0, result.exception
    assert "2 collections named 'rev [/draft]'" in result.output


def test_written_files_keep_their_mode_or_follow_the_umask(tmp_path):
    kept, new = tmp_path / "kept.yaml", tmp_path / "new.yaml"
    kept.write_text("old")
    kept.chmod(0o640)
    umask = os.umask(0o022)
    try:
        yamlio.write_text_atomic(kept, "a")
        yamlio.write_text_atomic(new, "b")
    finally:
        os.umask(umask)

    assert (kept.stat().st_mode & 0o777, new.stat().st_mode & 0o777) == (0o640, 0o644)


def test_rewriting_the_tree_keeps_files_that_are_not_yaml(tmp_path):
    collection_file(tmp_path, "Sales").rename(
        tmp_path / "collections" / "main" / "misplaced.yaml"
    )
    readme = tmp_path / "collections" / "main" / "README.md"
    readme.write_text("notes")

    workflow.fmt(tmp_path)

    assert (tmp_path / "collections" / "main" / "sales.yaml").exists()
    assert readme.read_text() == "notes"


def test_multiline_strings_are_literal_blocks_and_long_lines_never_wrap():
    sql = "SELECT *\nFROM orders\nWHERE " + "x = 1 AND " * 30 + "TRUE"
    text = yamlio.dumps({"native": sql, "description": "word " * 60})

    assert "native: |-\n  SELECT *\n  FROM orders\n" in text
    assert len(text.splitlines()) == 5
    assert yamlio.loads(text) == {"native": sql, "description": "word " * 60}


def test_a_clause_added_before_numbered_ones_keeps_every_reference_distinct():
    first = str(uuid.uuid5(UUID_NAMESPACE, "0"))
    added = "00000000-0000-4000-8000-000000000000"
    card = stable_uuids(
        {
            "dataset_query": {
                "stages": [
                    {
                        "aggregation": [
                            ["count", {"lib/uuid": added}],
                            ["sum", {"lib/uuid": first}, ["field", {}, 1]],
                        ],
                        "order-by": [
                            ["asc", {}, ["aggregation", {}, added]],
                            ["desc", {}, ["aggregation", {}, first]],
                        ],
                    }
                ]
            },
        }
    )

    stage = card["dataset_query"]["stages"][0]
    defined = [clause[1]["lib/uuid"] for clause in stage["aggregation"]]
    used = [clause[2][2] for clause in stage["order-by"]]
    assert len(set(defined)) == 2
    assert used == defined


def test_dump_drops_defaults_keeps_required_fields_and_sorts():
    dashboard = {
        "name": "D",
        "entity_id": "a" * 21,
        "auto_apply_filters": True,
        "width": "fixed",
        "tabs": [
            {"entity_id": "t2" + "x" * 19, "name": "Two", "position": 1},
            {"entity_id": "t1" + "x" * 19, "name": "One", "position": 0},
        ],
        "dashcards": [
            {
                "entity_id": "c" * 21,
                "dashboard_tab_id": ["a" * 21, "t2" + "x" * 19],
                "row": 0,
                "col": 0,
                "size_x": 1,
                "size_y": 1,
            },
            {
                "entity_id": "b" * 21,
                "dashboard_tab_id": ["a" * 21, "t1" + "x" * 19],
                "row": 5,
                "col": 0,
                "size_x": 1,
                "size_y": 1,
            },
            {
                "entity_id": "d" * 21,
                "dashboard_tab_id": ["a" * 21, "t1" + "x" * 19],
                "row": 0,
                "col": 3,
                "size_x": 1,
                "size_y": 1,
            },
        ],
    }

    out = representations.write(Dashboard.model_validate(dashboard))

    assert "auto_apply_filters" not in out and "width" not in out
    assert [t["name"] for t in out["tabs"]] == ["One", "Two"]
    assert [dc["entity_id"][0] for dc in out["dashcards"]] == ["d", "b", "c"]
    assert (out["dashcards"][0]["row"], out["tabs"][0]["position"]) == (0, 0)


def test_clashing_names_all_get_suffixes_whatever_the_order():
    a = tree.Named(entity_id="AAAAAAAAAAAAAAAAAAAA1", slug="twin")
    b = tree.Named(entity_id="BBBBBBBBBBBBBBBBBBBB2", slug="twin")
    c = tree.Named(entity_id="CCCCCCCCCCCCCCCCCCCC3", slug="solo")

    assert tree.assign_names([a, b, c]) == tree.assign_names([c, b, a])
    names = tree.assign_names([a, b, c])
    assert names[c.entity_id] == "solo"
    assert names[a.entity_id] != names[b.entity_id]
    assert all(n.startswith("twin__") for k, n in names.items() if k != c.entity_id)


def test_layout_nests_collections_dashboards_and_dashboard_questions():
    coll = Entity(kind=COLLECTION, data=Collection(name="Sales", entity_id="C" * 21))
    child = Entity(
        kind=COLLECTION,
        data=Collection(name="Q4", entity_id="D" * 21, parent_id="C" * 21),
    )
    dash = Entity(
        kind=DASHBOARD,
        data=Dashboard(name="Board", entity_id="E" * 21, collection_id="D" * 21),
    )
    question = Entity(
        kind=CARD,
        data=Card(
            name="Q",
            entity_id="F" * 21,
            collection_id="D" * 21,
            dashboard_id="E" * 21,
            display="table",
            dataset_query={},
            visualization_settings={},
        ),
    )
    sub = Entity(
        kind=SUBSCRIPTION,
        data=Subscription(
            entity_id="G" * 21,
            dashboard_id="E" * 21,
            channels=[Channel(channel_type="email")],
        ),
    )

    paths = tree.layout([sub, question, dash, child, coll])

    assert paths["C" * 21] == Path("collections/main/sales.yaml")
    assert paths["D" * 21] == Path("collections/main/sales/q4.yaml")
    assert paths["E" * 21] == Path("collections/main/sales/q4/board.yaml")
    assert paths["F" * 21] == Path("collections/main/sales/q4/board/q.yaml")
    assert paths["G" * 21] == Path("subscriptions/board.yaml")


def test_an_interrupted_write_loses_no_file_and_a_rerun_finishes_it(
    tmp_path, monkeypatch
):
    (tmp_path / "collections").mkdir()
    (tmp_path / "collections" / "old.yaml").write_text("old")
    rendered = {Path("collections/a.yaml"): "a", Path("collections/b.yaml"): "b"}
    real_replace = os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(yamlio.os, "replace", failing_replace)
    with pytest.raises(OSError):
        tree.write_tree(tmp_path, rendered, {}, {})
    assert (tmp_path / "collections" / "old.yaml").read_text() == "old"

    monkeypatch.setattr(yamlio.os, "replace", real_replace)
    tree.write_tree(tmp_path, rendered, {}, {})
    assert tree.current_files(tmp_path) == rendered
