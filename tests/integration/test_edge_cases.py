from datetime import datetime, timedelta
from pathlib import Path

import pytest

from metabase_sync.errors import (
    ApiError,
    ApplyStopped,
    ConflictError,
    NotKeptError,
    ValidationFailed,
)
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.ids import new_entity_id
from metabase_sync.metabase.client import UncertainWriteError
from metabase_sync.store import yamlio
from metabase_sync.store.journal import Journal, Started
from metabase_sync.store.state import STATE
from metabase_sync.store.tree import read_tree
from metabase_sync.sync import apply as executor_module
from metabase_sync.sync import commands as workflow

from ._content import build_rich, unique
from .test_roundtrip import describe, scope
from .test_scenarios import apply, assert_clean, exported, native_card, path_of

pytestmark = pytest.mark.integration


def add_card_file(root: Path, rich, name: str, sql: str, **extra) -> str:
    folder = path_of(root, rich.collection["entity_id"]).with_suffix("")
    yamlio.write(
        folder / f"{unique('new').replace(' ', '_')}.yaml",
        {
            **native_card(name, sql, rich.collection["entity_id"]),
            "entity_id": new_entity_id(),
            **extra,
        },
    )
    workflow.fmt(root)
    card = next(e for e in read_tree(root).entities.values() if e.name == name)
    return card.entity_id


def edit(path: Path, change) -> dict:
    data = yamlio.read(path)
    change(data)
    yamlio.write(path, data)
    return data


def test_a_change_metabase_silently_drops_fails_the_apply(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    edit(
        path_of(root, rich.dashboard["entity_id"]),
        lambda d: d["parameters"].append(
            {
                "id": "t1",
                "name": "Typo",
                "slug": "typo",
                "type": "category",
                "defualt": 1,
            }
        ),
    )

    with pytest.raises(NotKeptError) as raised:
        apply(client, root)
    _, again = workflow.plan(client, root)

    assert [d.entity_id for d in raised.value.items] == [rich.dashboard["entity_id"]]
    assert [op.entity_id for op in again.operations] == [rich.dashboard["entity_id"]]
    assert not again.drift


def test_restoring_a_subscription_keeps_skip_if_empty(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    pulse = f"/api/pulse/{rich.pulse['id']}"
    mb.put(pulse, {"skip_if_empty": True})
    workflow.export(client, root)
    mb.put(pulse, {"archived": True, "skip_if_empty": True})

    apply(client, root, force=True)

    restored = mb.get(pulse)
    assert (restored["archived"], restored["skip_if_empty"]) == (False, True)
    assert_clean(client, root)


def test_a_failed_apply_records_what_it_did_and_plans_the_rest(
    client, mb, tmp_path, monkeypatch
):
    rich, root = exported(client, mb, tmp_path)
    for card in (rich.native, rich.model):
        edit(path_of(root, card["entity_id"]), lambda d: d.update(description="new"))
    real_put = client.put
    refused = f"/api/card/{rich.model['id']}"

    def refuse_the_model(path, body):
        if path == refused:
            raise ApiError("PUT", path, 400, "refused")
        return real_put(path, body)

    monkeypatch.setattr(client, "put", refuse_the_model)
    with pytest.raises(ApplyStopped):
        apply(client, root)
    monkeypatch.setattr(client, "put", real_put)

    _, plan = workflow.plan(client, root)
    assert [op.entity_id for op in plan.operations] == [rich.model["entity_id"]]
    apply(client, root)
    assert_clean(client, root)


def test_a_new_question_on_an_existing_dashboard_is_placed_once(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    name = unique("Placed")
    card = add_card_file(
        root, rich, name, "SELECT 7", dashboard_id=rich.dashboard["entity_id"]
    )
    edit(
        path_of(root, rich.dashboard["entity_id"]),
        lambda d: d["dashcards"].append(
            {
                "entity_id": new_entity_id(),
                "dashboard_tab_id": [d["entity_id"], d["tabs"][0]["entity_id"]],
                "card_id": card,
                "row": 30,
                "col": 2,
                "size_x": 5,
                "size_y": 4,
            }
        ),
    )

    apply(client, root)

    created = next(c for c in mb.get("/api/card") if c["name"] == name)
    placed = [
        dc
        for dc in mb.get(f"/api/dashboard/{rich.dashboard['id']}")["dashcards"]
        if dc["card_id"] == created["id"]
    ]
    assert [(dc["row"], dc["col"], dc["size_x"]) for dc in placed] == [(30, 2, 5)]
    assert_clean(client, root)


def test_a_card_archived_in_the_ui_is_left_alone_and_export_takes_it(
    client, mb, tmp_path
):
    rich, root = exported(client, mb, tmp_path)
    mb.archive_card(rich.gui["id"])

    _, plan = workflow.plan(client, root)
    apply(client, root)
    result = workflow.export(client, root)

    assert plan.empty
    assert (rich.gui["entity_id"], "archived in Metabase") in [
        (d.entity_id, d.change) for d in plan.drift
    ]
    assert mb.get(f"/api/card/{rich.gui['id']}")["archived"] is True
    assert rich.gui["entity_id"] not in read_tree(root).entities
    assert any("orders_per_month" in p for p in result.files.removed)
    assert_clean(client, root)


def test_force_restores_a_card_archived_in_the_ui_with_its_dashcard(
    client, mb, tmp_path
):
    rich, root = exported(client, mb, tmp_path)
    mb.archive_card(rich.gui["id"])

    apply(client, root, force=True)

    assert mb.get(f"/api/card/{rich.gui['id']}")["archived"] is False
    assert rich.gui["id"] in [
        dc["card_id"]
        for dc in mb.get(f"/api/dashboard/{rich.dashboard['id']}")["dashcards"]
    ]
    assert_clean(client, root)


def test_force_restores_a_dashboard_archived_in_the_ui_once(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    mb.put(f"/api/dashboard/{rich.dashboard['id']}", {"archived": True})

    _, plan = workflow.plan(client, root, force=True)
    apply(client, root, force=True)

    assert ("restore", "dashboard") in [(op.action, op.kind) for op in plan.operations]
    restored = mb.get(f"/api/dashboard/{rich.dashboard['id']}")
    assert restored["archived"] is False
    assert restored["collection_id"] == rich.collection["id"]
    assert_clean(client, root)
    assert workflow.export(client, root, check=True).clean


def test_removing_a_subscription_on_a_dashboard_archived_in_the_ui_archives_it(
    client, mb, tmp_path
):
    rich, root = exported(client, mb, tmp_path)
    sub_path = next((root / "subscriptions").glob("*.yaml"))
    subscription = yamlio.read(sub_path)["entity_id"]
    mb.put(f"/api/dashboard/{rich.dashboard['id']}", {"archived": True})
    sub_path.unlink()

    _, plan = workflow.plan(client, root)

    assert f"{subscription}:base:archive" in [op.id for op in plan.operations]


def test_an_attachment_set_in_the_ui_survives_an_edit_from_the_files(
    client, mb, tmp_path
):
    rich, root = exported(client, mb, tmp_path)
    sub_path = path_of(root, rich.pulse["entity_id"])
    pulse_id = rich.pulse["id"]
    cards = mb.get(f"/api/pulse/{pulse_id}")["cards"]
    attached = cards[0]["dashboard_card_id"]
    mb.put(
        f"/api/pulse/{pulse_id}",
        {"cards": [{**cards[0], "include_csv": True}, *cards[1:]]},
    )
    workflow.export(client, root)
    edit(
        sub_path,
        lambda d: d["channels"][0]["recipients"].append("new@example.com"),
    )

    apply(client, root)

    after = mb.get(f"/api/pulse/{pulse_id}")["cards"]
    assert [c["include_csv"] for c in after if c["dashboard_card_id"] == attached] == [
        True
    ]
    assert_clean(client, root)


def test_hand_written_subscription_recipients_converge(client, mb, tmp_path):
    _, root = exported(client, mb, tmp_path)
    sub_path = next((root / "subscriptions").glob("*.yaml"))
    edit(
        sub_path,
        lambda d: d["channels"][0].update(
            recipients=["zoe@example.com", "Admin@Example.com", "Amy@example.com"]
        ),
    )

    apply(client, root)

    assert_clean(client, root)


def test_minimal_hand_written_files_converge(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    folder = path_of(root, rich.collection["entity_id"]).with_suffix("")
    model, board, tab = new_entity_id(), new_entity_id(), new_entity_id()
    yamlio.write(
        folder / "minimal_model.yaml",
        {
            **native_card(
                unique("Model"), "SELECT 1 AS one", rich.collection["entity_id"]
            ),
            "entity_id": model,
            "type": "model",
            "collection_preview": False,
        },
    )
    yamlio.write(
        folder / "minimal_board.yaml",
        {
            "name": unique("Board"),
            "entity_id": board,
            "collection_id": rich.collection["entity_id"],
            "width": "full",
            "auto_apply_filters": False,
            "tabs": [{"entity_id": tab, "name": "Only", "position": 0}],
            "dashcards": [
                {
                    "entity_id": new_entity_id(),
                    "dashboard_tab_id": [board, tab],
                    "card_id": model,
                    "row": 0,
                    "col": 0,
                    "size_x": 4,
                    "size_y": 3,
                }
            ],
            "serdes/meta": [{"model": "Dashboard"}],
        },
    )
    yamlio.write(
        root / "subscriptions" / "minimal.yaml",
        {
            "entity_id": new_entity_id(),
            "dashboard_id": board,
            "channels": [
                {
                    "channel_type": "email",
                    "schedule_type": "daily",
                    "schedule_hour": 8,
                    "recipients": ["someone@example.com"],
                }
            ],
            "serdes/meta": [{"model": "Pulse"}],
        },
    )
    workflow.fmt(root)

    apply(client, root)

    assert_clean(client, root)


def test_a_hand_added_query_clause_converges(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    gui_path = path_of(root, rich.gui["entity_id"])
    db, schema, _ = yamlio.read(gui_path)["dataset_query"]["stages"][0]["source-table"]
    edit(
        gui_path,
        lambda d: d["dataset_query"]["stages"][0]["aggregation"].insert(
            0,
            [
                "sum",
                {},
                ["field", {"base-type": "type/Float"}, [db, schema, "ORDERS", "TOTAL"]],
            ],
        ),
    )

    apply(client, root)

    stage = mb.get(f"/api/card/{rich.gui['id']}")["dataset_query"]["stages"][0]
    assert len(stage["aggregation"]) == 2
    mb.run_card_query(rich.gui["id"])
    assert_clean(client, root)


def test_a_new_filter_can_take_its_values_from_a_new_card(
    client, mb, tmp_path, reversed_levels
):
    rich, root = exported(client, mb, tmp_path)
    name = unique("Categories")
    card = add_card_file(root, rich, name, "SELECT DISTINCT CATEGORY FROM PRODUCTS")
    edit(
        path_of(root, rich.dashboard["entity_id"]),
        lambda d: d["parameters"].append(
            {
                "id": "c1",
                "name": "Category",
                "slug": "category",
                "type": "string/=",
                "values_source_type": "card",
                "values_source_config": {
                    "card_id": card,
                    "value_field": ["field", "CATEGORY", {"base-type": "type/Text"}],
                },
            }
        ),
    )

    apply(client, root)

    created = next(c for c in mb.get("/api/card") if c["name"] == name)
    parameter = next(
        p
        for p in mb.get(f"/api/dashboard/{rich.dashboard['id']}")["parameters"]
        if p["id"] == "c1"
    )
    assert parameter["values_source_config"]["card_id"] == created["id"]
    assert_clean(client, root)


def test_editing_cards_that_link_to_each_other(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    for card in (rich.native, rich.derived):
        edit(
            path_of(root, card["entity_id"]),
            lambda d: d.update(description=unique("edited")),
        )

    _, plan = workflow.plan(client, root)
    apply(client, root)

    assert sorted(op.name for op in plan.operations) == sorted(
        [rich.native["name"], rich.derived["name"]]
    )
    assert_clean(client, root)


def test_a_reference_to_a_field_that_no_longer_exists_is_kept(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    mb.update_card(
        rich.native["id"],
        {
            "visualization_settings": {
                "column_settings": {
                    '["ref",["field",999999,null]]': {"column_title": "Gone"}
                }
            }
        },
    )

    result = workflow.export(client, root)

    assert any(
        f"{rich.native['name']!r} refers to field 999999," in w for w in result.broken
    )
    assert_clean(client, root)


def test_a_link_card_points_at_the_question_by_entity_id(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    dash = yamlio.read(path_of(root, rich.dashboard["entity_id"]))
    links = [
        dc["visualization_settings"]["link"]["entity"]["id"]
        for dc in dash["dashcards"]
        if (dc.get("visualization_settings") or {}).get("link")
    ]

    assert links == [rich.derived["entity_id"]]


def test_a_lost_create_is_found_even_when_the_local_clock_runs_ahead(
    client, mb, tmp_path, monkeypatch
):
    rich, root = exported(client, mb, tmp_path)
    name = unique("Skewed")
    add_card_file(root, rich, name, "SELECT 1")

    class Ahead(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=10)

    monkeypatch.setattr(executor_module, "datetime", Ahead, raising=False)
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

    assert len([c for c in mb.get("/api/card") if c["name"] == name]) == 1
    assert_clean(client, root)


def test_a_create_still_in_flight_is_never_sent_twice(
    client, mb, tmp_path, monkeypatch
):
    rich, root = exported(client, mb, tmp_path)
    name = unique("Slow create")
    add_card_file(root, rich, name, "SELECT 1")
    real_post = client.post
    in_flight = []

    def slow_post(path, body, read_only=False):
        if path == "/api/card" and not in_flight:
            in_flight.append(body)
            raise UncertainWriteError("simulated gateway timeout")
        return real_post(path, body, read_only=read_only)

    monkeypatch.setattr(client, "post", slow_post)

    with pytest.raises(UncertainWriteError):
        apply(client, root)
    real_post("/api/card", in_flight[0])
    apply(client, root)

    assert len([c for c in mb.get("/api/card") if c["name"] == name]) == 1
    assert_clean(client, root)


def test_an_apply_interrupted_between_a_dashboards_parts_finishes_next_time(
    client, mb, tmp_path, monkeypatch
):
    rich, root = exported(client, mb, tmp_path)
    renamed = unique("Renamed")

    def rename_and_resize(data):
        data["name"] = renamed
        data["dashcards"][0]["size_x"] += 1

    edit(path_of(root, rich.dashboard["entity_id"]), rename_and_resize)
    real_record = Journal.record

    def crash_before_contents(self, entry):
        if isinstance(entry, Started) and entry.op.endswith(":contents:update"):
            raise KeyboardInterrupt
        real_record(self, entry)

    monkeypatch.setattr(Journal, "record", crash_before_contents)
    with pytest.raises(KeyboardInterrupt):
        apply(client, root)
    monkeypatch.setattr(Journal, "record", real_record)

    apply(client, root)

    assert mb.get(f"/api/dashboard/{rich.dashboard['id']}")["name"] == renamed
    assert_clean(client, root)


def test_two_files_matching_one_item_stop_as_conflicts(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    original = path_of(root, rich.model["entity_id"])
    data = yamlio.read(original)
    original.unlink()
    (root / STATE).unlink()
    for suffix in ("a", "b"):
        yamlio.write(
            original.with_name(f"copy_{suffix}.yaml"),
            {**data, "entity_id": new_entity_id()},
        )

    _, plan = workflow.plan(client, root)

    assert sorted(Path(str(c.path)).name for c in plan.conflicts) == [
        "copy_a.yaml",
        "copy_b.yaml",
    ]
    assert not [op for op in plan.operations if op.path and "copy_" in op.path]
    assert rich.model["entity_id"] not in plan.renames.values()


def test_export_before_apply_keeps_a_hand_written_file_for_an_existing_item(
    client, mb, tmp_path
):
    rich, root = exported(client, mb, tmp_path)
    original = path_of(root, rich.model["entity_id"])
    data = yamlio.read(original)
    original.unlink()
    (root / STATE).unlink()
    hand = original.with_name("by_hand.yaml")
    yamlio.write(hand, {**data, "entity_id": new_entity_id(), "description": "by hand"})

    workflow.export(client, root)

    named = [
        (e.path, e.data.description)
        for e in read_tree(root).entities.values()
        if isinstance(e.data, Card) and e.name == data["name"]
    ]
    assert named == [(hand.relative_to(root), "by hand")]
    apply(client, root)
    cards = [
        c
        for c in mb.get("/api/card")
        if c["name"] == data["name"] and c["collection_id"] == rich.collection["id"]
    ]
    assert [c["description"] for c in cards] == ["by hand"]


def test_a_question_inside_a_document_is_left_alone_and_listed_as_unmanaged(
    client, mb, tmp_path
):
    rich, root = exported(client, mb, tmp_path)
    mb.post(
        "/api/document",
        {
            "name": unique("Notes"),
            "collection_id": rich.collection["id"],
            "document": {"type": "doc", "content": []},
            "cards": {
                "-1": {
                    "name": "Inside the notes",
                    "display": "scalar",
                    "visualization_settings": {},
                    "dataset_query": {
                        "database": mb.sample_db_id(),
                        "type": "native",
                        "native": {"query": "SELECT 1"},
                    },
                }
            },
        },
    )

    result = workflow.export(client, root)
    _, plan = workflow.plan(client, root)

    assert result.clean
    assert plan.empty
    assert "Inside the notes" in [ref.name for ref in plan.unmanaged]


def test_a_whole_tree_written_without_ids_imports_by_name(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    local = read_tree(root)
    mapping = {}
    for e in local.entities.values():
        mapping[e.entity_id] = new_entity_id()
        if isinstance(e.data, Dashboard):
            for nested in [*e.data.tabs, *e.data.dashcards]:
                mapping[nested.entity_id] = new_entity_id()
    executor_module.rename_ids(root, mapping)
    yamlio.write(
        root / "metabase-sync.yaml", {"collections": [rich.collection["entity_id"]]}
    )
    (root / STATE).unlink()

    done = apply(client, root)

    assert {action for action, _, _ in done} == {"import"}
    _, plan = workflow.plan(client, root)
    assert plan.empty, describe(plan)
    assert set(read_tree(root).entities) == set(local.entities)


def test_export_force_never_deletes_a_file_it_cannot_read(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    broken = path_of(root, rich.native["entity_id"]).with_name("draft.yaml")
    broken.write_text("name: [unclosed\n", encoding="utf-8")

    with pytest.raises(ValidationFailed):
        workflow.export(client, root, force=True)

    assert broken.exists()


def test_export_keeps_edits_and_new_files_you_have_not_applied(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    native = path_of(root, rich.native["entity_id"])
    edit(native, lambda d: d.update(description="not applied yet"))
    folder = path_of(root, rich.collection["entity_id"]).with_suffix("")
    yamlio.write(
        folder / "draft.yaml",
        native_card("Draft", "SELECT 5", rich.collection["entity_id"]),
    )
    mb.update_card(rich.derived["id"], {"description": "edited in the UI"})

    result = workflow.export(client, root)

    assert yamlio.read(native)["description"] == "not applied yet"
    assert (folder / "draft.yaml").exists()
    assert str(native.relative_to(root)) in result.kept
    assert yamlio.read(path_of(root, rich.derived["entity_id"]))["description"] == (
        "edited in the UI"
    )


def test_export_refuses_a_conflict_and_force_takes_metabase(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    native = path_of(root, rich.native["entity_id"])
    edit(native, lambda d: d.update(description="edited in the files"))
    mb.update_card(rich.native["id"], {"description": "edited in the UI"})

    refused = workflow.export(client, root)
    assert yamlio.read(native)["description"] == "edited in the files"
    workflow.export(client, root, force=True)

    assert [c.change for c in refused.conflicts] == ["changed here and in Metabase"]
    assert yamlio.read(native)["description"] == "edited in the UI"
    assert_clean(client, root)


def test_apply_leaves_an_edit_made_in_the_ui_alone(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    edit(
        path_of(root, rich.native["entity_id"]),
        lambda d: d.update(description="edited in the files"),
    )
    mb.update_card(rich.derived["id"], {"description": "edited in the UI"})

    _, plan = workflow.plan(client, root)
    apply(client, root)

    assert [op.name for op in plan.operations] == [rich.native["name"]]
    assert [d.entity_id for d in plan.drift] == [rich.derived["entity_id"]]
    assert mb.get(f"/api/card/{rich.derived['id']}")["description"] == (
        "edited in the UI"
    )
    _, after = workflow.plan(client, root)
    assert after.empty and [d.entity_id for d in after.drift] == [
        rich.derived["entity_id"]
    ]


def test_apply_refuses_a_conflict_until_forced(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    edit(
        path_of(root, rich.native["entity_id"]),
        lambda d: d.update(description="edited in the files"),
    )
    mb.update_card(rich.native["id"], {"description": "edited in the UI"})

    with pytest.raises(ConflictError):
        apply(client, root)
    assert mb.get(f"/api/card/{rich.native['id']}")["description"] == (
        "edited in the UI"
    )
    apply(client, root, force=True)

    assert mb.get(f"/api/card/{rich.native['id']}")["description"] == (
        "edited in the files"
    )
    assert_clean(client, root)


def test_a_new_file_without_ids_applies_and_converges(client, mb, tmp_path):
    rich, root = exported(client, mb, tmp_path)
    folder = path_of(root, rich.collection["entity_id"]).with_suffix("")
    name = unique("Bare")
    yamlio.write(
        folder / "bare.yaml",
        native_card(name, "SELECT 9", rich.collection["entity_id"]),
    )

    _, plan = workflow.plan(client, root)
    apply(client, root)

    created = next(c for c in mb.get("/api/card") if c["name"] == name)
    assert [(op.action, op.name) for op in plan.operations] == [("create", name)]
    assert yamlio.read(folder / "bare.yaml")["entity_id"] == created["entity_id"]
    assert_clean(client, root)
    _, after = workflow.plan(client, root)
    assert not after.drift


def test_removal_delete_deletes_only_what_the_plan_names(client, mb, tmp_path):
    rich = build_rich(mb)
    home = rich.collection["id"]
    card = mb.create_native_card(unique("Doomed card"), "SELECT 1", collection_id=home)
    board = mb.create_dashboard(unique("Doomed board"), collection_id=home)
    doomed = mb.create_collection(unique("Doomed"), parent_id=home)
    inner = mb.create_native_card(
        unique("Inner"), "SELECT 2", collection_id=doomed["id"]
    )
    holder = mb.create_collection(unique("Holder"), parent_id=home)
    hidden = mb.create_native_card(
        unique("Hidden"), "SELECT 3", collection_id=holder["id"]
    )
    mb.archive_card(hidden["id"])
    root = scope(tmp_path / "root", rich.collection["entity_id"])
    yamlio.write(
        root / "metabase-sync.yaml",
        {"collections": [rich.collection["entity_id"]], "removal": "delete"},
    )
    workflow.export(client, root)
    for item in (card, board, inner, doomed, holder):
        path_of(root, item["entity_id"]).unlink()

    _, plan = workflow.plan(client, root, force=True)
    apply(client, root, force=True)

    deleted = [card, board, inner, doomed]
    assert sorted(op.entity_id for op in plan.operations if op.action == "delete") == (
        sorted(item["entity_id"] for item in deleted)
    )
    assert [c.entity_id for c in plan.conflicts] == [holder["entity_id"]]
    assert hidden["name"] in plan.conflicts[0].change
    gone = [
        f"/api/card/{card['id']}",
        f"/api/dashboard/{board['id']}",
        f"/api/card/{inner['id']}",
        f"/api/collection/{doomed['id']}",
    ]
    assert [mb.status(path) for path in gone] == [404] * 4
    assert mb.status(f"/api/card/{hidden['id']}") == 200


def test_removal_delete_keeps_subscriptions_and_timelines_it_does_not_manage(
    client, mb, tmp_path
):
    rich = build_rich(mb)
    home = rich.collection["id"]
    board = mb.create_dashboard(unique("Watched board"), collection_id=home)
    shown = mb.create_native_card(unique("Shown"), "SELECT 1", collection_id=home)
    dashcard = mb.set_dashboard_contents(
        board["id"],
        [
            {
                "id": -1,
                "card_id": shown["id"],
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
            }
        ],
    )["dashcards"][0]
    dated = mb.create_collection(unique("Dated"), parent_id=home)
    root = scope(tmp_path / "root", rich.collection["entity_id"])
    yamlio.write(
        root / "metabase-sync.yaml",
        {"collections": [rich.collection["entity_id"]], "removal": "delete"},
    )
    workflow.export(client, root)
    pulse = mb.create_pulse(
        board["name"],
        dashboard_id=board["id"],
        collection_id=None,
        cards=[
            {
                "id": shown["id"],
                "dashboard_card_id": dashcard["id"],
                "include_csv": False,
                "include_xls": False,
            }
        ],
        channels=[
            {
                "channel_type": "email",
                "enabled": True,
                "schedule_type": "daily",
                "schedule_hour": 8,
                "recipients": [{"email": "someone@example.com"}],
            }
        ],
    )
    timeline = mb.post(
        "/api/timeline", {"name": "Launches", "collection_id": dated["id"]}
    )
    for item in (board, dated):
        path_of(root, item["entity_id"]).unlink()

    _, plan = workflow.plan(client, root, force=True)
    apply(client, root, force=True)

    assert sorted(c.entity_id for c in plan.conflicts) == sorted(
        [board["entity_id"], dated["entity_id"]]
    )
    assert not [op for op in plan.operations if op.action == "delete"]
    kept = [
        f"/api/dashboard/{board['id']}",
        f"/api/pulse/{pulse['id']}",
        f"/api/collection/{dated['id']}",
        f"/api/timeline/{timeline['id']}",
    ]
    assert [mb.status(path) for path in kept] == [200] * 4


def test_an_action_button_survives_an_update_from_the_files(client, mb, tmp_path):
    rich = build_rich(mb)
    database = mb.post(
        "/api/database",
        {
            "engine": "postgres",
            "name": unique("Zz actions"),
            "details": {
                "host": "postgres",
                "port": 5432,
                "dbname": "metabase",
                "user": "metabase",
                "password": "metabase",
            },
            "is_full_sync": False,
        },
    )
    mb.put(
        f"/api/database/{database['id']}",
        {"settings": {"database-enable-actions": True}},
    )
    query = {
        "database": database["id"],
        "type": "native",
        "native": {"query": "SELECT 1 AS x"},
    }
    model = mb.post(
        "/api/card",
        {
            "name": unique("Actions model"),
            "type": "model",
            "display": "table",
            "dataset_query": query,
            "visualization_settings": {},
        },
    )
    action = mb.post(
        "/api/action",
        {
            "name": "Go",
            "type": "query",
            "model_id": model["id"],
            "database_id": database["id"],
            "dataset_query": query,
            "parameters": [],
            "visualization_settings": {},
        },
    )
    board = mb.create_dashboard(unique("Buttons"), collection_id=rich.collection["id"])
    mb.set_dashboard_contents(
        board["id"],
        [
            {
                "id": -1,
                "action_id": action["id"],
                "card_id": None,
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 1,
                "visualization_settings": {"actionDisplayType": "button"},
            }
        ],
    )
    root = scope(tmp_path / "root", rich.collection["entity_id"])
    result = workflow.export(client, root)
    edit(
        path_of(root, board["entity_id"]),
        lambda d: d["dashcards"][0].update(row=2),
    )

    apply(client, root)

    dashcards = mb.get(f"/api/dashboard/{board['id']}")["dashcards"]
    assert any("action button" in w for w in result.warnings)
    assert [(dc["row"], dc["action_id"]) for dc in dashcards] == [(2, action["id"])]
