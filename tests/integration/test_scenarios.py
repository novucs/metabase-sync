import shutil
from pathlib import Path
from typing import Any

import pytest

from metabase_sync.errors import StalePlanError, StateError
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.ids import new_entity_id
from metabase_sync.metabase.client import UncertainWriteError
from metabase_sync.store import yamlio
from metabase_sync.store.journal import Created, Journal
from metabase_sync.store.state import STATE
from metabase_sync.store.tree import read_tree
from metabase_sync.sync import apply as executor_module
from metabase_sync.sync import commands as workflow

from ._content import build_rich, unique
from .test_roundtrip import describe, scope

pytestmark = pytest.mark.integration


def exported(client, mb, tmp_path):
    rich = build_rich(mb)
    root = scope(tmp_path / "root", rich.collection["entity_id"])
    workflow.export(client, root)
    return rich, root


def path_of(root: Path, entity_id: str) -> Path:
    entity = read_tree(root).get(entity_id)
    assert entity is not None and entity.path is not None
    return root / entity.path


def apply(client, root: Path, plan_file: Path | None = None, force: bool = False):
    done: list[tuple[str, str, str]] = []
    prepared, result = workflow.plan_apply(client, root, plan_file, force)
    workflow.apply(
        client,
        root,
        prepared,
        result,
        lambda op, d: done.append((op.action, op.kind, op.name)),
    )
    return done


def assert_clean(client, root: Path) -> None:
    _, plan = workflow.plan(client, root)
    assert plan.empty, describe(plan)
    assert not plan.drift and not plan.conflicts, "".join(plan.diffs.values())


def native_card(name: str, sql: str, collection: str) -> dict[str, Any]:
    return {
        "name": name,
        "collection_id": collection,
        "display": "scalar",
        "dataset_query": {
            "lib/type": "mbql/query",
            "database": "Sample Database",
            "stages": [{"lib/type": "mbql.stage/native", "native": sql}],
        },
        "visualization_settings": {},
        "serdes/meta": [{"model": "Card"}],
    }


def test_a_gui_question_with_aggregation_references_round_trips_through_apply(
    client, mb, tmp_path
):
    sample = next(
        c for c in mb.get("/api/card") if c["name"] == "Best selling products"
    )
    scope_all = tmp_path / "all"
    scope_all.mkdir()
    yamlio.write(scope_all / "metabase-sync.yaml", {"removal": "archive"})
    workflow.export(client, scope_all)
    path = path_of(scope_all, sample["entity_id"])
    data = yamlio.read(path)
    data["description"] = unique("edited")
    yamlio.write(path, data)

    _, plan = workflow.plan(client, scope_all)
    apply(client, scope_all)

    assert [(op.action, op.name) for op in plan.operations] == [
        ("update", "Best selling products")
    ]
    assert mb.get(f"/api/card/{sample['id']}")["description"] == data["description"]
    mb.run_card_query(sample["id"])
    assert_clean(client, scope_all)
    assert workflow.export(client, scope_all, check=True).clean


def test_editing_sql_updates_only_that_card(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    path = path_of(root, rich.native["entity_id"])
    data = yamlio.read(path)
    data["dataset_query"]["stages"][0]["native"] = data["dataset_query"]["stages"][0][
        "native"
    ].replace("count(*)", "count(*) AS orders")
    yamlio.write(path, data)

    _, plan = workflow.plan(client, root)
    apply(client, root)

    assert describe(plan) == [("update", "card", "Orders by date", ["dataset_query"])]
    assert (
        "AS orders"
        in mb.get(f"/api/card/{rich.native['id']}")["dataset_query"]["stages"][0][
            "native"
        ]
    )
    assert_clean(client, root)


def test_new_files_are_created_once_and_take_metabase_ids(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    collection_dir = path_of(root, rich.collection["entity_id"]).with_suffix("")
    name = unique("Answer")
    yamlio.write(
        collection_dir / "answer.yaml",
        native_card(name, "SELECT 42", rich.collection["entity_id"]),
    )
    workflow.fmt(root)
    card = next(e for e in read_tree(root).entities.values() if e.name == name)
    dash_path = path_of(root, rich.dashboard["entity_id"])
    dash = yamlio.read(dash_path)
    dash["dashcards"].append(
        {
            "card_id": card.entity_id,
            "dashboard_tab_id": [dash["entity_id"], dash["tabs"][0]["entity_id"]],
            "row": 20,
            "col": 0,
            "size_x": 4,
            "size_y": 3,
        }
    )
    yamlio.write(dash_path, dash)
    workflow.fmt(root)
    provisional = card.entity_id

    _, plan = workflow.plan(client, root)
    apply(client, root)
    apply(client, root)

    assert [(op.action, op.kind) for op in plan.operations] == [
        ("create", "card"),
        ("update", "dashboard"),
    ]
    remote = [c for c in mb.get("/api/card") if c["name"] == name]
    assert len(remote) == 1
    local = read_tree(root)
    assert provisional not in local.entities
    assert remote[0]["entity_id"] in local.entities
    remote_dash = mb.get(f"/api/dashboard/{rich.dashboard['id']}")
    local_dash = local.get(rich.dashboard["entity_id"])
    assert local_dash is not None and isinstance(local_dash.data, Dashboard)
    assert {dc["entity_id"] for dc in remote_dash["dashcards"]} == {
        dc.entity_id for dc in local_dash.data.dashcards
    }
    assert_clean(client, root)


def test_a_nested_collection_can_be_managed_on_its_own(client, mb, tmp_path):
    rich = build_rich(mb)
    root = scope(tmp_path / "root", rich.child["entity_id"])
    workflow.export(client, root)
    local = read_tree(root)
    card_path = path_of(root, rich.gui["entity_id"])
    card = yamlio.read(card_path)
    card["name"] = unique("Renamed")
    yamlio.write(card_path, card)
    yamlio.write(
        card_path.with_name("answer.yaml"),
        native_card(unique("Answer"), "SELECT 42", rich.child["entity_id"]),
    )
    workflow.fmt(root)

    done = apply(client, root)

    assert rich.collection["entity_id"] not in local.entities
    assert rich.native["entity_id"] not in local.entities
    assert sorted((action, kind) for action, kind, _ in done) == [
        ("create", "card"),
        ("update", "card"),
    ]
    assert mb.get_card(rich.gui["id"])["name"] == card["name"]
    assert_clean(client, root)


def test_removing_a_card_and_its_dashcard_together(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    path_of(root, rich.gui["entity_id"]).unlink()
    dash_path = path_of(root, rich.dashboard["entity_id"])
    dash = yamlio.read(dash_path)
    dash["dashcards"] = [
        dc for dc in dash["dashcards"] if dc.get("card_id") != rich.gui["entity_id"]
    ]
    yamlio.write(dash_path, dash)

    _, plan = workflow.plan(client, root)
    apply(client, root)

    assert [(op.action, op.kind) for op in plan.operations] == [
        ("update", "dashboard"),
        ("archive", "card"),
    ]
    assert mb.get(f"/api/card/{rich.gui['id']}")["archived"] is True
    assert_clean(client, root)
    assert workflow.export(client, root, check=True).clean


def remove_child_collection(root: Path, rich) -> None:
    child = path_of(root, rich.child["entity_id"])
    shutil.rmtree(child.with_suffix(""))
    child.unlink()
    dash_path = path_of(root, rich.dashboard["entity_id"])
    dash = yamlio.read(dash_path)
    dash["dashcards"] = [
        dc for dc in dash["dashcards"] if dc.get("card_id") != rich.gui["entity_id"]
    ]
    yamlio.write(dash_path, dash)


def test_removing_a_collection_archives_it_once_with_its_contents(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    remove_child_collection(root, rich)

    _, plan = workflow.plan(client, root)
    apply(client, root)

    assert [(op.action, op.kind) for op in plan.operations] == [
        ("update", "dashboard"),
        ("archive", "collection"),
    ]
    assert [c.entity_id for c in plan.cascaded] == [rich.gui["entity_id"]]
    assert mb.get(f"/api/collection/{rich.child['id']}")["archived"] is True
    assert mb.get(f"/api/card/{rich.gui['id']}")["archived"] is True
    assert_clean(client, root)


def test_restoring_files_restores_the_archived_collection(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    backup = tmp_path / "backup"
    shutil.copytree(root, backup)
    remove_child_collection(root, rich)
    apply(client, root)
    shutil.rmtree(root)
    shutil.copytree(backup, root)

    _, held = workflow.plan(client, root)
    _, plan = workflow.plan(client, root, force=True)
    apply(client, root, force=True)

    assert held.empty
    assert rich.child["entity_id"] in {d.entity_id for d in held.drift}
    assert ("restore", "collection") in [(op.action, op.kind) for op in plan.operations]
    assert mb.get(f"/api/collection/{rich.child['id']}")["archived"] is False
    assert mb.get(f"/api/card/{rich.gui['id']}")["archived"] is False
    assert_clean(client, root)


def test_a_saved_plan_is_refused_once_metabase_changes(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    path = path_of(root, rich.native["entity_id"])
    data = yamlio.read(path)
    data["description"] = "planned"
    yamlio.write(path, data)
    _, plan = workflow.plan(client, root)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())
    mb.put(f"/api/card/{rich.native['id']}", {"description": "changed in the UI"})

    with pytest.raises(StalePlanError):
        apply(client, root, plan_file)
    assert (
        mb.get(f"/api/card/{rich.native['id']}")["description"] == "changed in the UI"
    )


def test_a_saved_plan_is_refused_once_the_files_change(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    path = path_of(root, rich.native["entity_id"])
    data = yamlio.read(path)
    data["description"] = "reviewed"
    yamlio.write(path, data)
    _, plan = workflow.plan(client, root)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.model_dump_json())
    data["description"] = "edited after review"
    yamlio.write(path, data)

    with pytest.raises(StalePlanError):
        apply(client, root, plan_file)


def test_a_create_whose_response_is_lost_is_not_duplicated(
    client, mb, tmp_path, monkeypatch
):
    rich, root = exported(client, mb, tmp_path)
    collection_dir = path_of(root, rich.collection["entity_id"]).with_suffix("")
    name = unique("Lost response")
    yamlio.write(
        collection_dir / "lost.yaml",
        native_card(name, "SELECT 1", rich.collection["entity_id"]),
    )
    workflow.fmt(root)
    real_post = client.post
    calls = {"n": 0}

    def flaky_post(path, body):
        calls["n"] += 1
        result = real_post(path, body)
        if calls["n"] == 1:
            raise UncertainWriteError("simulated lost response")
        return result

    monkeypatch.setattr(client, "post", flaky_post)

    apply(client, root)

    assert calls["n"] == 1
    assert len([c for c in mb.get("/api/card") if c["name"] == name]) == 1
    assert_clean(client, root)


def test_a_lost_create_is_told_apart_from_a_sibling_created_before_it(
    client, mb, tmp_path, monkeypatch
):
    rich, root = exported(client, mb, tmp_path)
    for hour in (6, 18):
        yamlio.write(
            root / "subscriptions" / f"at_{hour}.yaml",
            {
                "dashboard_id": rich.dashboard["entity_id"],
                "channels": [
                    {
                        "channel_type": "email",
                        "schedule_type": "daily",
                        "schedule_hour": hour,
                        "recipients": ["admin@example.com"],
                    }
                ],
                "serdes/meta": [{"model": "Pulse"}],
            },
        )
    workflow.fmt(root)
    real_post = client.post
    pulses = {"n": 0}

    def second_pulse_lost(path, body, **kwargs):
        result = real_post(path, body, **kwargs)
        if path == "/api/pulse":
            pulses["n"] += 1
            if pulses["n"] == 2:
                raise UncertainWriteError("simulated lost response")
        return result

    monkeypatch.setattr(client, "post", second_pulse_lost)

    apply(client, root)

    on_board = [
        p for p in mb.get("/api/pulse") if p["dashboard_id"] == rich.dashboard["id"]
    ]
    assert len(on_board) == 3
    assert_clean(client, root)


def test_an_apply_interrupted_after_a_create_resumes_without_duplicating(
    client, mb, tmp_path, monkeypatch
):
    rich, root = exported(client, mb, tmp_path)
    collection_dir = path_of(root, rich.collection["entity_id"]).with_suffix("")
    name = unique("Interrupted")
    yamlio.write(
        collection_dir / "interrupted.yaml",
        native_card(name, "SELECT 2", rich.collection["entity_id"]),
    )
    workflow.fmt(root)
    real_record = Journal.record

    def crash_after_post(self, entry):
        if isinstance(entry, Created):
            raise KeyboardInterrupt
        real_record(self, entry)

    monkeypatch.setattr(Journal, "record", crash_after_post)
    with pytest.raises(KeyboardInterrupt):
        apply(client, root)
    monkeypatch.setattr(Journal, "record", real_record)

    with pytest.raises(StateError, match="interrupted"):
        workflow.plan(client, root)
    prepared, plan = workflow.plan_apply(client, root, None)

    assert any("interrupted apply" in note for note in prepared.notes)
    assert plan.empty, describe(plan)
    assert len([c for c in mb.get("/api/card") if c["name"] == name]) == 1


def test_duplicate_names_get_stable_file_names(client, mb, tmp_path):
    rich = build_rich(mb)
    for _ in range(2):
        mb.create_native_card("Twin", "SELECT 3", collection_id=rich.collection["id"])
    root = scope(tmp_path / "root", rich.collection["entity_id"])
    workflow.export(client, root)
    first = sorted(
        p.name
        for p in path_of(root, rich.collection["entity_id"])
        .with_suffix("")
        .glob("twin*.yaml")
    )
    shutil.rmtree(root / "collections")
    workflow.export(client, root, force=True)
    second = sorted(
        p.name
        for p in path_of(root, rich.collection["entity_id"])
        .with_suffix("")
        .glob("twin*.yaml")
    )

    assert len(first) == 2 and first[0] != first[1]
    assert first == second


def test_files_apply_by_name_to_an_instance_without_their_ids(
    client, mb, tmp_path, reversed_levels
):
    rich, root = exported(client, mb, tmp_path)
    local = read_tree(root)
    mapping = {}
    for e in local.entities.values():
        if e.kind != "snippet":
            mapping[e.entity_id] = new_entity_id()
        if isinstance(e.data, Dashboard):
            for nested in [*e.data.tabs, *e.data.dashcards]:
                mapping[nested.entity_id] = new_entity_id()
    executor_module.rename_ids(root, mapping)
    collection_file = path_of(root, mapping[rich.collection["entity_id"]])
    data = yamlio.read(collection_file)
    data["name"] = unique("Copy")
    yamlio.write(collection_file, data)
    yamlio.write(
        root / "metabase-sync.yaml",
        {"collections": [mapping[rich.collection["entity_id"]]]},
    )
    (root / STATE).unlink()

    done = apply(client, root)

    assert len([d for d in done if d[0] == "create"]) >= 9
    assert (
        mapping[rich.collection["entity_id"]]
        not in (root / "metabase-sync.yaml").read_text()
    )
    assert_clean(client, root)
    copies = {
        e.name: e.entity_id
        for e in read_tree(root).entities.values()
        if e.kind in ("card", "dashboard")
    }
    by_eid = {c["entity_id"]: c for c in mb.get("/api/card")}
    derived_copy = by_eid[copies[rich.derived["name"]]]
    native_copy = by_eid[copies[rich.native["name"]]]
    assert (
        native_copy["visualization_settings"]["click_behavior"]["targetId"]
        == derived_copy["id"]
    )
    dashboard_copy = next(
        d for d in mb.get("/api/dashboard") if d["entity_id"] == copies["Overview"]
    )
    links = [
        dc["visualization_settings"]["link"]["entity"]["id"]
        for dc in mb.get(f"/api/dashboard/{dashboard_copy['id']}")["dashcards"]
        if dc["visualization_settings"].get("link")
    ]
    assert links == [derived_copy["id"]]
