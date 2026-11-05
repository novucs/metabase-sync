from pathlib import Path
from typing import Any

import pytest

from metabase_sync.errors import ValidationFailed
from metabase_sync.format import representations
from metabase_sync.format.api import v58
from metabase_sync.format.representations import (
    CARD,
    COLLECTION,
    DASHBOARD,
    SUBSCRIPTION,
)
from metabase_sync.format.representations.card import Card
from metabase_sync.format.representations.dashboard import Dashboard
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.metabase.index import Index
from metabase_sync.metabase.remote import Remote
from metabase_sync.store.state import Managed
from metabase_sync.store.tree import Entity, LocalTree
from metabase_sync.sync.operations import local_hash
from metabase_sync.sync.order import stages
from metabase_sync.sync.plan import build_plan


def eid(tag: str) -> str:
    return (tag * 21)[:21]


def remote(**items: list[dict[str, Any]]) -> Index:
    index = Index(version="v0.64.4", metadata=None, databases={1: "Warehouse"})  # type: ignore[arg-type]
    for kind, entries in items.items():
        for entry in entries:
            index.add(kind, entry)
    return index


def connected(index: Index) -> Remote:
    return Remote(index=index, api=v58)


def card(id_: int, tag: str, collection: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "id": id_,
        "entity_id": eid(tag),
        "name": f"card {tag}",
        "collection_id": collection,
        "display": "table",
        "database_id": 1,
        "dataset_query": {
            "lib/type": "mbql/query",
            "database": 1,
            "stages": [{"lib/type": "mbql.stage/native", "native": "SELECT 1"}],
        },
        "visualization_settings": {},
        "updated_at": "2026-01-01T00:00:00Z",
        **extra,
    }


def collection(id_: int, tag: str, location: str = "/", **extra: Any) -> dict[str, Any]:
    return {
        "id": id_,
        "entity_id": eid(tag),
        "name": f"coll {tag}",
        "location": location,
        "updated_at": "2026-01-01",
        **extra,
    }


def local_from(
    index: Index, kinds: list[tuple[str, int]], managed: set[str]
) -> LocalTree:
    metabase = connected(index)
    local = LocalTree(root=Path("."), state={e: Managed(kind="card") for e in managed})
    for kind, item_id in kinds:
        item = index.get(kind, item_id)
        assert item is not None
        data = metabase.item(kind, item)
        local.entities[data.entity_id] = Entity(
            kind=kind, data=data, path=Path(f"{data.entity_id}.yaml")
        )
    return local


def edit(local: LocalTree, tag: str, **changes: Any) -> None:
    entity = local.entities[eid(tag)]
    entity.data = entity.data.model_copy(update=changes)


def plan_for(index: Index, local: LocalTree, force: bool = False):
    return build_plan(local, connected(index), force=force)


def test_matching_files_plan_nothing():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a"), eid("c")})

    assert plan_for(index, local).empty


def test_removing_a_collection_archives_only_the_collection():
    index = remote(
        collection=[collection(1, "a"), collection(2, "b", "/1/")],
        card=[card(10, "c", 2), card(11, "d", 1)],
    )
    local = local_from(index, [(COLLECTION, 1), (CARD, 11)], {eid(t) for t in "abcd"})

    plan = plan_for(index, local)

    assert [(op.action, op.kind, op.entity_id) for op in plan.operations] == [
        ("archive", "collection", eid("b"))
    ]
    assert [c.entity_id for c in plan.cascaded] == [eid("c")]


def test_removing_a_collection_that_holds_unmanaged_items_stops_until_forced():
    index = remote(
        collection=[collection(1, "a"), collection(2, "b", "/1/")],
        card=[card(10, "c", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("b")})

    held = plan_for(index, local)
    forced = plan_for(index, local, force=True)

    assert held.empty
    assert [(c.entity_id, c.change) for c in held.conflicts] == [
        (eid("b"), "removed here, but holds 1 item that is not in the files")
    ]
    assert [op.entity_id for op in forced.operations] == [eid("b")]
    assert [c.entity_id for c in forced.cascaded] == [eid("c")]


def test_items_never_seen_by_the_tool_are_left_alone():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = plan_for(index, local)

    assert plan.empty
    assert [u.entity_id for u in plan.unmanaged] == [eid("c")]


def unmanaged_card_rewritten_as(index: Index, tag: str) -> LocalTree:
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a")})
    local.entities[eid(tag)] = local.entities.pop(eid("c"))
    edit(local, tag, entity_id=eid(tag))
    return local


def test_a_new_file_for_an_existing_item_imports_it_instead_of_a_duplicate():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = unmanaged_card_rewritten_as(index, "z")
    edit(local, "z", description="from the files")

    plan = plan_for(index, local)

    assert plan.renames == {eid("z"): eid("c")}
    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("import", eid("c")),
        ("update", eid("c")),
    ]


def test_a_new_file_matching_two_items_stops_unless_forced_to_create_another():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(10, "c", 1), card(11, "d", 1, name="card c")],
    )
    local = unmanaged_card_rewritten_as(index, "z")

    plan = plan_for(index, local)
    forced = plan_for(index, local, force=True)

    assert not plan.renames and plan.empty
    assert [c.entity_id for c in plan.conflicts] == [eid("z")]
    assert [op.action for op in forced.operations] == ["create"]


def board_with_slack_subscription() -> Index:
    return remote(
        collection=[collection(1, "a")],
        dashboard=[
            {
                "id": 20,
                "entity_id": eid("b"),
                "name": "board",
                "collection_id": 1,
                "updated_at": "2026-01-01",
            }
        ],
        subscription=[
            {
                "id": 30,
                "entity_id": eid("s"),
                "name": "board",
                "dashboard_id": 20,
                "channels": [
                    {
                        "channel_type": "slack",
                        "details": {"channel": "#sales"},
                        "recipients": [],
                    }
                ],
                "updated_at": "2026-01-01",
            }
        ],
    )


@pytest.mark.parametrize(
    ("channel", "action"),
    [
        ({"channel_type": "email", "recipients": ["a@example.com"]}, "create"),
        ({"channel_type": "slack", "details": {"channel": "#sales"}}, "import"),
    ],
)
def test_a_new_subscription_imports_only_one_with_the_same_channels(channel, action):
    index = board_with_slack_subscription()
    local = local_from(index, [(COLLECTION, 1), (DASHBOARD, 20)], {eid("a"), eid("b")})
    local.entities[eid("n")] = Entity(
        kind=SUBSCRIPTION,
        data=Subscription.model_validate(
            {"entity_id": eid("n"), "dashboard_id": eid("b"), "channels": [channel]}
        ),
        path=Path("n.yaml"),
    )

    actions = [op.action for op in plan_for(index, local).operations]

    assert actions[0] == action


def test_an_archived_item_with_a_file_is_restored_never_recreated():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(10, "c", 1, archived=True, archived_directly=True)],
    )
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a"), eid("c")})

    plan = plan_for(index, local)

    assert [(op.action, op.kind) for op in plan.operations] == [("restore", "card")]


def test_restoring_a_collection_re_archives_children_whose_files_are_gone():
    index = remote(
        collection=[collection(1, "a", archived=True, archived_directly=True)],
        card=[
            card(10, "c", 1, archived=True, archived_directly=False),
            card(11, "d", 1, archived=True, archived_directly=False),
        ],
    )
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a"), eid("c")})
    edit(local, "c", name="renamed")

    plan = plan_for(index, local)
    actions = [(op.action, op.kind, op.entity_id) for op in plan.operations]

    assert actions.index(("restore", "collection", eid("a"))) < actions.index(
        ("archive", "card", eid("d"))
    )
    assert ("restore", "card", eid("c")) in actions
    update = next(op for op in plan.operations if op.action == "update")
    assert f"{eid('c')}:base:restore" in update.depends_on


def test_new_items_are_created_after_what_they_reference():
    index = remote()
    local = LocalTree(root=Path("."))
    coll = {
        "name": "New",
        "entity_id": eid("n"),
        "serdes/meta": [{"model": "Collection"}],
    }
    child_card = {
        "name": "Child",
        "entity_id": eid("k"),
        "collection_id": eid("n"),
        "display": "table",
        "dataset_query": {
            "lib/type": "mbql/query",
            "database": "Warehouse",
            "stages": [{"lib/type": "mbql.stage/native", "native": "SELECT 1"}],
        },
        "visualization_settings": {},
    }
    board = {
        "name": "Board",
        "entity_id": eid("b"),
        "collection_id": eid("n"),
        "dashcards": [
            {
                "entity_id": eid("x"),
                "card_id": eid("k"),
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
            }
        ],
    }
    for kind, data in (
        (DASHBOARD, board),
        (CARD, child_card),
        (COLLECTION, coll),
    ):
        local.entities[data["entity_id"]] = Entity(
            kind=kind,
            data=representations.read(data, kind, "test"),
            path=Path(f"{data['entity_id']}.yaml"),
        )

    order = [op.id for op in plan_for(index, local).operations]

    assert order.index(f"{eid('n')}:base:create") < order.index(
        f"{eid('k')}:base:create"
    )
    assert order.index(f"{eid('k')}:base:create") < order.index(
        f"{eid('b')}:contents:create"
    )
    assert order.index(f"{eid('b')}:base:create") < order.index(
        f"{eid('b')}:contents:create"
    )


def test_removals_that_reference_nothing_being_removed_run_together():
    index = remote(
        collection=[collection(1, "a"), collection(2, "b")],
        card=[card(10, "c", 1), card(11, "d", 1), card(12, "e", 1)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid(t) for t in "abcde"})

    grouped = stages(plan_for(index, local).operations)

    assert [sorted(op.kind for op in stage) for stage in grouped] == [
        ["card", "card", "card", "collection"]
    ]


def test_a_new_card_that_links_to_the_card_its_query_uses_is_created_after_it():
    index = remote(collection=[collection(1, "a")])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    for tag, other, query in (
        ("p", "q", {"lib/type": "mbql.stage/mbql", "source-card": eid("q")}),
        ("q", "p", {"lib/type": "mbql.stage/native", "native": "SELECT 1"}),
    ):
        local.entities[eid(tag)] = Entity(
            kind=CARD,
            data=Card(
                name=f"card {tag}",
                entity_id=eid(tag),
                collection_id=eid("a"),
                display="table",
                dataset_query={
                    "lib/type": "mbql/query",
                    "database": "Warehouse",
                    "stages": [query],
                },
                visualization_settings={
                    "click_behavior": {
                        "type": "link",
                        "linkType": "question",
                        "targetId": eid(other),
                    }
                },
            ),
            path=Path(f"card_{tag}.yaml"),
        )

    order = [op.id for op in plan_for(index, local).operations]

    assert order.index(f"{eid('q')}:base:create") < order.index(
        f"{eid('p')}:base:create"
    )


def test_new_pinned_items_are_created_in_their_pinned_order():
    index = remote(collection=[collection(1, "a")])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    for kind, item in (
        (
            CARD,
            Card(
                name="card",
                entity_id=eid("p"),
                collection_id=eid("a"),
                collection_position=2,
                display="table",
                dataset_query={},
                visualization_settings={},
            ),
        ),
        (
            DASHBOARD,
            Dashboard(
                name="board",
                entity_id=eid("q"),
                collection_id=eid("a"),
                collection_position=1,
            ),
        ),
    ):
        local.entities[item.entity_id] = Entity(
            kind=kind, data=item, path=Path(item.name)
        )

    order = [op.id for op in plan_for(index, local).operations]

    assert order.index(f"{eid('q')}:base:create") < order.index(
        f"{eid('p')}:base:create"
    )


def test_items_that_need_each_other_first_are_reported_not_crashed_on():
    index = remote(collection=[collection(1, "a")])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    for tag, other in (("p", "q"), ("q", "p")):
        local.entities[eid(tag)] = Entity(
            kind=CARD,
            data=Card(
                name=f"card {tag}",
                entity_id=eid(tag),
                collection_id=eid("a"),
                display="table",
                dataset_query={
                    "lib/type": "mbql/query",
                    "database": "Warehouse",
                    "stages": [
                        {"lib/type": "mbql.stage/mbql", "source-card": eid(other)}
                    ],
                },
                visualization_settings={},
            ),
            path=Path(f"card_{tag}.yaml"),
        )

    with pytest.raises(ValidationFailed) as raised:
        plan_for(index, local)

    assert "card_p.yaml" in raised.value.problems[0]


def managed_tree(index: Index) -> LocalTree:
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a"), eid("c")})
    local.state = {
        e.entity_id: Managed(kind=e.kind, hash=local_hash(e))
        for e in local.entities.values()
    }
    return local


def test_an_edit_made_in_metabase_is_reported_not_reverted():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = managed_tree(index)
    index.add(CARD, card(10, "c", 1, description="edited in the UI"))

    plan = plan_for(index, local)

    assert plan.empty
    assert [(d.entity_id, d.change) for d in plan.drift] == [
        (eid("c"), "changed in Metabase")
    ]
    assert "edited in the UI" in plan.diffs[eid("c")]


def test_a_different_creator_is_not_a_change():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(10, "c", 1, creator={"email": "first@example.com"})],
    )
    local = managed_tree(index)
    edit(local, "c", creator_id="second@example.com")

    plan = plan_for(index, local)

    assert plan.empty
    assert not plan.drift


def test_disable_links_in_a_file_is_never_planned_because_metabase_cannot_set_it():
    index = board_with_slack_subscription()
    local = local_from(
        index,
        [(COLLECTION, 1), (DASHBOARD, 20), (SUBSCRIPTION, 30)],
        {eid("a"), eid("b"), eid("s")},
    )
    edit(local, "s", disable_links=True)

    assert plan_for(index, local).empty


def test_an_edit_here_is_applied_and_shown_as_a_diff():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = managed_tree(index)
    edit(local, "c", description="edited in the files")

    plan = plan_for(index, local)

    assert [(op.action, op.changes) for op in plan.operations] == [
        ("update", ["description"])
    ]
    assert "+description: edited in the files" in plan.diffs[eid("c")]


def test_edits_on_both_sides_are_a_conflict_until_forced():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = managed_tree(index)
    edit(local, "c", description="edited in the files")
    index.add(CARD, card(10, "c", 1, description="edited in the UI"))

    held = plan_for(index, local)
    forced = plan_for(index, local, force=True)

    assert held.empty
    assert [c.entity_id for c in held.conflicts] == [eid("c")]
    assert [op.action for op in forced.operations] == ["update"]


def test_removing_a_file_whose_item_changed_in_metabase_is_a_conflict():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = managed_tree(index)
    del local.entities[eid("c")]
    index.add(CARD, card(10, "c", 1, description="edited in the UI"))

    plan = plan_for(index, local)

    assert plan.empty
    assert [c.change for c in plan.conflicts] == ["removed here, changed in Metabase"]


def test_removing_a_model_written_without_its_columns_archives_it():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(10, "c", 1, type="model", result_metadata=[{"name": "ID"}])],
    )
    local = managed_tree(index)
    edit(local, "c", result_metadata=None)
    local.state[eid("c")] = Managed(
        kind="card", hash=local_hash(local.entities[eid("c")])
    )
    del local.entities[eid("c")]

    plan = plan_for(index, local)

    assert not plan.conflicts
    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("archive", eid("c"))
    ]


def test_removing_the_dashcard_a_filter_sits_on_asks_to_remove_the_filter_too():
    index = remote(
        collection=[collection(1, "a")],
        dashboard=[
            {
                "id": 20,
                "entity_id": eid("b"),
                "name": "board",
                "collection_id": 1,
                "parameters": [{"id": "d1", "name": "Region", "type": "category"}],
                "dashcards": [
                    {
                        "id": 5,
                        "entity_id": eid("x"),
                        "row": 0,
                        "col": 0,
                        "size_x": 4,
                        "size_y": 1,
                        "inline_parameters": ["d1"],
                    }
                ],
                "updated_at": "2026-01-01",
            }
        ],
    )
    local = local_from(index, [(COLLECTION, 1), (DASHBOARD, 20)], {eid("a"), eid("b")})
    edit(local, "b", dashcards=[])

    with pytest.raises(ValidationFailed) as raised:
        plan_for(index, local)

    assert "'Region'" in raised.value.problems[0]


def test_an_item_deleted_in_metabase_is_not_recreated():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = managed_tree(index)
    del index.items[CARD][10]
    del index._by_eid[eid("c")]

    plan = plan_for(index, local)

    assert plan.empty
    assert [d.change for d in plan.drift] == ["deleted in Metabase"]


def test_the_fingerprint_changes_when_a_file_changes_after_planning():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a"), eid("c")})
    edit(local, "c", description="first")
    first = plan_for(index, local).fingerprint
    edit(local, "c", description="second")

    assert plan_for(index, local).fingerprint != first


def test_a_file_moved_outside_the_configured_collections_is_refused():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    index.add_outside(COLLECTION, 5, eid("z"))
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a"), eid("c")})
    edit(local, "c", collection_id=eid("z"))

    with pytest.raises(ValidationFailed) as raised:
        build_plan(local, connected(index), scope={eid("a")})

    assert "outside the collections" in raised.value.problems[0]


def test_a_file_reusing_the_entity_id_of_another_kind_is_refused():
    index = remote(collection=[collection(1, "a")])
    local = LocalTree(root=Path("."), state={})
    local.entities[eid("a")] = Entity(
        kind=CARD,
        data=Card(
            name="card",
            entity_id=eid("a"),
            display="table",
            dataset_query={},
            visualization_settings={},
        ),
        path=Path("card.yaml"),
    )

    with pytest.raises(ValidationFailed) as raised:
        plan_for(index, local)

    assert "belongs to a collection" in raised.value.problems[0]


def test_new_items_with_the_same_name_and_place_are_created_one_at_a_time():
    index = remote(collection=[collection(1, "a")])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    for tag in "pq":
        local.entities[eid(tag)] = Entity(
            kind=CARD,
            data=Card(
                name="same",
                entity_id=eid(tag),
                collection_id=eid("a"),
                display="table",
                dataset_query={},
                visualization_settings={},
            ),
            path=Path(f"same_{tag}.yaml"),
        )

    grouped = stages(plan_for(index, local).operations)

    assert [[op.entity_id for op in stage] for stage in grouped] == [
        [eid("p")],
        [eid("q")],
    ]
