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
from metabase_sync.format.representations.dashboard import Dashboard, Dashcard
from metabase_sync.format.representations.subscription import Subscription
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.metabase.index import Index, RemoteItem
from metabase_sync.metabase.metadata import Metadata
from metabase_sync.metabase.remote import Remote
from metabase_sync.settings import Settings
from metabase_sync.store import yamlio
from metabase_sync.store.journal import Journal, Posting, Started
from metabase_sync.store.state import STATE, Managed
from metabase_sync.store.tree import CONFIG, Entity, LocalTree, render, write_tree
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.apply import reconcile
from metabase_sync.sync.operations import local_hash
from metabase_sync.sync.order import stages
from metabase_sync.sync.plan import build_plan


def eid(tag: str) -> str:
    return (tag * 21)[:21]


def remote(**items: list[dict[str, Any]]) -> Index:
    databases = {"Warehouse": [1]}
    index = Index(
        version="v0.64.4",
        metadata=Metadata(offline_client(), databases),
        databases=databases,
    )
    for kind, entries in items.items():
        for entry in entries:
            index.add(kind, entry)
    return index


def connected(index: Index) -> Remote:
    return Remote(index=index, api=v58)


def offline_client() -> MetabaseClient:
    return MetabaseClient(
        Settings.model_validate({"url": "http://mb.test", "api_key": "k"})
    )


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
        (eid("b"), "removed here, but holds card 'card c', which is not in the files")
    ]
    assert [op.entity_id for op in forced.operations] == [eid("b")]
    assert [c.entity_id for c in forced.cascaded] == [eid("c")]


def test_items_never_seen_by_the_tool_are_left_alone():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = plan_for(index, local)

    assert plan.empty
    assert [u.entity_id for u in plan.unmanaged] == [eid("c")]


@pytest.mark.parametrize("mode", ["archive", "delete"])
def test_unmanaged_items_are_removed_when_configured(mode):
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged=mode)

    assert [(op.action, op.entity_id, op.reason) for op in plan.operations] == [
        (mode, eid("c"), "only in Metabase")
    ]
    assert plan.unmanaged == []


def test_an_unmanaged_collection_is_archived_once_with_its_contents():
    index = remote(
        collection=[collection(1, "a"), collection(2, "b", "/1/")],
        card=[card(10, "c", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged="archive")

    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("archive", eid("b"))
    ]
    assert [c.entity_id for c in plan.cascaded] == [eid("c")]


@pytest.mark.parametrize("moved", [False, True])
def test_a_metabase_collection_holding_a_managed_item_goes_once_it_is_moved_out(
    moved,
):
    index = remote(
        collection=[collection(1, "a"), collection(2, "b", "/1/")],
        card=[card(10, "c", 2)],
    )
    local = local_from(index, [(COLLECTION, 1), (CARD, 10)], {eid("a"), eid("c")})
    if moved:
        edit(local, "c", collection_id=eid("a"))

    plan = build_plan(local, connected(index), unmanaged="archive")

    actions = {(op.action, op.entity_id) for op in plan.operations}
    if moved:
        assert actions == {("update", eid("c")), ("archive", eid("b"))}
    else:
        assert actions == set()
        assert [(k.entity_id, k.reason) for k in plan.kept] == [
            (eid("b"), f"used by {eid('c')}.yaml")
        ]


def test_metabase_only_items_a_document_uses_are_kept():
    index = remote(
        collection=[collection(1, "a")],
        card=[
            card(11, "m", 1),
            card(12, "s", 1),
            card(
                10,
                "d",
                1,
                document_id=5,
                dataset_query={
                    "lib/type": "mbql/query",
                    "database": 1,
                    "stages": [{"lib/type": "mbql.stage/mbql", "source-card": 11}],
                },
            ),
        ],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("delete", eid("s"))
    ]
    assert [(k.entity_id, k.reason) for k in plan.kept] == [
        (eid("m"), "used by card 'card d'")
    ]
    assert [u.entity_id for u in plan.unmanaged] == [eid("d")]


def test_a_metabase_collection_is_kept_when_its_managed_dashboard_cannot_be_deleted():
    index = board_with_slack_subscription()
    index.add(COLLECTION, collection(2, "t", "/1/"))
    index.add(DASHBOARD, {**index.raw[DASHBOARD, 20], "collection_id": 2})
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("b")})

    plan = build_plan(
        local, connected(index), removal="delete", unmanaged="archive", force=True
    )

    assert not [op for op in plan.operations if op.entity_id == eid("t")]
    assert eid("b") in [c.entity_id for c in plan.blocked]
    assert eid("t") in [k.entity_id for k in plan.kept]


def test_items_a_managed_item_uses_in_metabase_are_kept():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(11, "y", 1)],
        dashboard=[
            {
                "id": 20,
                "entity_id": eid("b"),
                "name": "board",
                "collection_id": 1,
                "dashcards": [
                    {
                        "id": 1,
                        "entity_id": eid("x"),
                        "card_id": 11,
                        "row": 0,
                        "col": 0,
                        "size_x": 4,
                        "size_y": 4,
                    }
                ],
                "updated_at": "2026-01-01",
            }
        ],
    )
    local = local_from(index, [(COLLECTION, 1), (DASHBOARD, 20)], {eid("a"), eid("b")})
    edit(local, "b", dashcards=[])
    local.state[eid("b")] = Managed("dashboard", local_hash(local.entities[eid("b")]))

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert eid("b") in [d.entity_id for d in plan.drift]

    assert eid("y") not in [op.entity_id for op in plan.operations]
    assert (eid("y"), "used by dashboard 'board'") in [
        (k.entity_id, k.reason) for k in plan.kept
    ]


def test_trashed_items_do_not_stop_deleting_a_metabase_collection():
    index = remote(
        collection=[collection(1, "a"), collection(2, "s", "/1/")],
        card=[
            card(10, "c", 2),
            card(11, "z", 2, archived=True, archived_directly=True),
        ],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert not plan.conflicts
    assert {(op.action, op.entity_id) for op in plan.operations} == {
        ("delete", eid("s")),
        ("delete", eid("c")),
    }


def test_forcing_never_archives_a_kept_item_with_its_collection():
    index = remote(
        collection=[collection(1, "a"), collection(2, "m", "/1/")],
        card=[card(10, "x", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("m")})
    entity = new_card_in(
        eid("a"), {"lib/type": "mbql.stage/mbql", "source-card": eid("x")}
    )
    local.entities[eid("n")] = entity

    plan = build_plan(local, connected(index), unmanaged="archive", force=True)

    assert eid("m") not in [op.entity_id for op in plan.operations]
    assert [(c.entity_id, c.change) for c in plan.blocked] == [
        (eid("m"), "removed here, but holds card 'card x'")
    ]
    assert eid("x") in [k.entity_id for k in plan.kept]
    assert eid("x") not in [c.entity_id for c in plan.cascaded]


def new_card_in(collection_eid: str, stage: dict[str, Any] | None = None) -> Entity:
    data = {
        "name": "New",
        "entity_id": eid("n"),
        "collection_id": collection_eid,
        "display": "table",
        "dataset_query": {
            "lib/type": "mbql/query",
            "database": "Warehouse",
            "stages": [
                stage or {"lib/type": "mbql.stage/native", "native": "SELECT 1"}
            ],
        },
        "visualization_settings": {},
    }
    return Entity(
        kind=CARD, data=representations.read(data, CARD, "test"), path=Path("n.yaml")
    )


def test_unmanaged_items_the_files_use_are_kept():
    index = remote(
        collection=[
            collection(1, "a"),
            collection(2, "e", "/1/"),
            collection(3, "b", "/1/2/"),
        ],
        card=[card(10, "d", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    local.entities[eid("n")] = new_card_in(eid("b"))

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("create", eid("n"))
    ]
    assert not plan.conflicts
    assert {(k.entity_id, k.reason) for k in plan.kept} == {
        (eid("b"), "used by n.yaml"),
        (eid("e"), "holds collection 'coll b'"),
        (eid("d"), "inside collection 'coll e', which is kept"),
    }
    assert plan.unmanaged == []


def test_a_metabase_collection_holding_a_managed_item_being_removed_goes_too():
    index = remote(
        collection=[collection(1, "a"), collection(2, "s", "/1/")],
        card=[card(10, "m", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("m")})

    plan = build_plan(local, connected(index), unmanaged="archive")

    assert not plan.conflicts
    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("archive", eid("s"))
    ]
    assert [c.entity_id for c in plan.cascaded] == [eid("m")]


def test_removing_a_collection_whose_contents_are_removed_anyway_is_not_a_conflict():
    index = remote(
        collection=[collection(1, "a"), collection(2, "x", "/1/")],
        card=[card(10, "y", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("x")})

    plan = build_plan(local, connected(index), unmanaged="archive")

    assert not plan.conflicts
    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("archive", eid("x"))
    ]
    assert [c.entity_id for c in plan.cascaded] == [eid("y")]


def holding_a_document() -> Index:
    index = remote(collection=[collection(1, "a"), collection(2, "x", "/1/")])
    index.documents.append(
        RemoteItem.model_validate(
            {"id": 7, "entity_id": eid("d"), "name": "notes", "collection_id": 2}
        )
    )
    return index


@pytest.mark.parametrize("force", [False, True])
def test_removing_a_collection_holding_a_document_is_blocked(force):
    index = holding_a_document()
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("x")})

    plan = build_plan(local, connected(index), force=force)

    assert plan.empty
    assert [(c.entity_id, c.change) for c in plan.blocked] == [
        (eid("x"), "removed here, but holds document 'notes'")
    ]


def test_a_metabase_collection_holding_a_document_is_kept():
    index = holding_a_document()
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert plan.empty
    assert not plan.conflicts
    assert [(k.entity_id, k.reason) for k in plan.kept] == [
        (eid("x"), "holds document 'notes'")
    ]


def test_a_metabase_collection_holding_a_timeline_is_kept():
    index = remote(collection=[collection(1, "a"), collection(2, "x", "/1/")])
    index.timelines.append(
        RemoteItem.model_validate(
            {"id": 3, "entity_id": eid("r"), "name": "releases", "collection_id": 2}
        )
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged="archive")

    assert plan.empty
    assert [(k.entity_id, k.reason) for k in plan.kept] == [
        (eid("x"), "holds timeline 'releases'")
    ]


def test_a_kept_dashboard_keeps_its_subscriptions():
    index = board_with_slack_subscription()
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    entity = new_card_in(eid("a"))
    entity.data = entity.data.model_copy(update={"dashboard_id": eid("b")})
    local.entities[eid("n")] = entity

    plan = build_plan(local, connected(index), unmanaged="archive")

    assert {(k.entity_id, k.reason) for k in plan.kept} == {
        (eid("b"), "used by n.yaml"),
        (eid("s"), "inside dashboard 'board', which is kept"),
    }


def test_what_a_kept_dashboard_shows_is_kept():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(11, "y", 1)],
        dashboard=[
            {
                "id": 20,
                "entity_id": eid("b"),
                "name": "board",
                "collection_id": 1,
                "dashcards": [
                    {
                        "id": 1,
                        "entity_id": eid("x"),
                        "card_id": 11,
                        "row": 0,
                        "col": 0,
                        "size_x": 4,
                        "size_y": 4,
                    }
                ],
                "updated_at": "2026-01-01",
            }
        ],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    entity = new_card_in(eid("a"))
    entity.data = entity.data.model_copy(update={"dashboard_id": eid("b")})
    local.entities[eid("n")] = entity

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert [op.action for op in plan.operations] == ["create"]
    assert {(k.entity_id, k.reason) for k in plan.kept} == {
        (eid("b"), "used by n.yaml"),
        (eid("y"), "used by dashboard 'board'"),
    }


def board_showing(card_id: int) -> dict[str, Any]:
    return {
        "id": 20,
        "entity_id": eid("b"),
        "name": "board",
        "collection_id": 1,
        "dashcards": [
            {
                "id": 1,
                "entity_id": eid("x"),
                "card_id": card_id,
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
            }
        ],
        "updated_at": "2026-01-01",
    }


@pytest.mark.parametrize("force", [False, True])
def test_removing_a_question_a_metabase_only_dashboard_shows_needs_force(force):
    index = remote(
        collection=[collection(1, "a")],
        card=[card(10, "q", 1)],
        dashboard=[board_showing(10)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("q")})

    plan = build_plan(local, connected(index), force=force)

    assert not plan.blocked
    if force:
        assert [(op.action, op.entity_id) for op in plan.operations] == [
            ("archive", eid("q"))
        ]
    else:
        change = "removed here, but is used by dashboard 'board', which is not in"
        assert plan.empty
        assert [(c.entity_id, c.change) for c in plan.conflicts] == [
            (eid("q"), f"{change} the files")
        ]


def test_removing_a_question_a_managed_dashboard_shows_is_blocked():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(10, "q", 1)],
        dashboard=[board_showing(10)],
    )
    local = local_from(
        index, [(COLLECTION, 1), (DASHBOARD, 20)], {eid("a"), eid("q"), eid("b")}
    )

    plan = build_plan(local, connected(index), force=True)

    assert [(c.entity_id, c.change) for c in plan.blocked] == [
        (eid("q"), f"removed here, but is used by {eid('b')}.yaml")
    ]


def test_deleting_never_destroys_what_the_plan_only_archives():
    index = remote(
        collection=[collection(1, "a"), collection(2, "x", "/1/")],
        card=[card(10, "y", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("x")})

    plan = build_plan(local, connected(index), removal="delete", unmanaged="archive")

    assert eid("x") not in [op.entity_id for op in plan.operations]
    assert [c.entity_id for c in plan.blocked] == [eid("x")]
    assert "deleting it would also delete card 'card y'" in plan.blocked[0].change


@pytest.mark.parametrize(
    ("scope", "root_question", "removed"),
    [(None, True, True), (None, False, False), ({eid("a")}, False, False)],
)
def test_the_root_is_managed_only_once_a_whole_instance_repo_exports_it(
    scope, root_question, removed
):
    index = remote(
        collection=[collection(1, "a"), collection(2, "o")], card=[card(10, "r", None)]
    )
    files = [(COLLECTION, 1), (CARD, 10)] if root_question else [(COLLECTION, 1)]
    managed = {eid("a"), eid("r")} if root_question else {eid("a")}
    local = local_from(index, files, managed)

    plan = build_plan(local, connected(index), scope=scope, unmanaged="archive")

    assert (eid("o") in [op.entity_id for op in plan.operations]) is removed
    assert (eid("o") in [u.entity_id for u in plan.unmanaged]) is not removed


def test_forcing_lists_what_goes_with_the_archived_collection():
    index = remote(
        collection=[collection(1, "a"), collection(2, "c", "/1/")],
        card=[card(10, "q", 2)],
        dashboard=[{**board_showing(10), "collection_id": None}],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("c")})

    plan = build_plan(local, connected(index), unmanaged="archive", force=True)

    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("archive", eid("c"))
    ]
    assert [c.entity_id for c in plan.cascaded] == [eid("q")]
    assert eid("q") not in [k.entity_id for k in plan.kept]


def test_nothing_is_swept_along_with_a_blocked_removal():
    index = holding_a_document()
    index.add(CARD, card(10, "q", 2))
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("x")})

    plan = build_plan(local, connected(index), force=True)

    assert plan.empty
    assert [c.entity_id for c in plan.blocked] == [eid("x")]
    assert eid("q") in [u.entity_id for u in plan.unmanaged]


@pytest.mark.parametrize("force", [False, True])
def test_an_edit_made_in_metabase_never_blocks_its_collection(force):
    index = remote(
        collection=[collection(1, "a"), collection(2, "c", "/1/")],
        card=[card(10, "m", 2)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("c"), eid("m")})
    local.state[eid("m")] = Managed("card", "edited since")

    plan = build_plan(local, connected(index), force=force)

    assert not plan.blocked
    if force:
        assert [(op.action, op.entity_id) for op in plan.operations] == [
            ("archive", eid("c"))
        ]
    else:
        assert {c.entity_id for c in plan.conflicts} == {eid("m"), eid("c")}


def test_deleting_a_dashboard_takes_its_own_questions_with_it():
    index = remote(
        collection=[collection(1, "a")],
        card=[card(11, "q", 1, dashboard_id=20)],
        dashboard=[board_showing(11)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("delete", eid("b"))
    ]
    assert [c.entity_id for c in plan.cascaded] == [eid("q")]


def test_deleting_a_collection_keeps_its_trashed_timeline_and_lists_trashed_items():
    index = remote(
        collection=[collection(1, "a"), collection(2, "x", "/1/")],
        card=[card(10, "z", 2, archived=True, archived_directly=True)],
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("x")})
    timeline = {"id": 3, "entity_id": eid("r"), "name": "releases"}
    index.timelines.append(
        RemoteItem.model_validate({**timeline, "collection_id": 2, "archived": True})
    )

    blocked = build_plan(local, connected(index), removal="delete", unmanaged="delete")
    index.timelines.clear()
    plan = build_plan(local, connected(index), removal="delete", unmanaged="delete")

    assert "timeline 'releases'" in blocked.blocked[0].change
    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("delete", eid("x"))
    ]
    assert [c.entity_id for c in plan.cascaded] == [eid("z")]


def test_a_metabase_only_subscription_goes_with_its_removed_dashboard():
    index = board_with_slack_subscription()
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("b")})

    plan = build_plan(local, connected(index))

    assert not plan.conflicts and not plan.blocked
    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("archive", eid("b"))
    ]


def test_a_repo_that_has_never_synced_removes_nothing():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = local_from(index, [], set())

    plan = build_plan(local, connected(index), unmanaged="delete")

    assert plan.empty
    assert not plan.kept
    assert {u.entity_id for u in plan.unmanaged} == {eid("a"), eid("c")}


def test_unmanaged_items_are_archived_even_when_removal_deletes():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), removal="delete", unmanaged="archive")

    assert [op.action for op in plan.operations] == ["archive"]


def test_questions_inside_documents_are_never_removed():
    index = remote(
        collection=[collection(1, "a")], card=[card(10, "c", 1, document_id=5)]
    )
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})

    plan = build_plan(local, connected(index), unmanaged="delete")

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


def test_a_dashboard_whose_subscription_was_archived_can_be_deleted():
    index = board_with_slack_subscription()
    index.add(SUBSCRIPTION, {**index.raw[SUBSCRIPTION, 30], "archived": True})
    local = local_from(index, [(COLLECTION, 1)], {eid("a"), eid("b")})

    plan = build_plan(local, connected(index), removal="delete")

    assert not plan.conflicts
    assert [(op.action, op.kind) for op in plan.operations] == [("delete", "dashboard")]


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


def test_a_pinned_question_on_a_card_pinned_after_it_is_created_after_that_card():
    index = remote(collection=[collection(1, "a")])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    for tag, pin, stage in (
        ("m", 2, {"lib/type": "mbql.stage/native", "native": "SELECT 1"}),
        ("q", 1, {"lib/type": "mbql.stage/mbql", "source-card": eid("m")}),
    ):
        local.entities[eid(tag)] = Entity(
            kind=CARD,
            data=Card(
                name=f"card {tag}",
                entity_id=eid(tag),
                collection_id=eid("a"),
                collection_position=pin,
                display="table",
                dataset_query={
                    "lib/type": "mbql/query",
                    "database": "Warehouse",
                    "stages": [stage],
                },
                visualization_settings={},
            ),
            path=Path(f"card_{tag}.yaml"),
        )

    order = [op.id for op in plan_for(index, local).operations]

    assert order.index(f"{eid('m')}:base:create") < order.index(
        f"{eid('q')}:base:create"
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


def model_index() -> Index:
    return remote(
        collection=[collection(1, "a")],
        card=[card(10, "c", 1, type="model", result_metadata=[{"name": "ID"}])],
    )


def written_without_columns(index: Index) -> LocalTree:
    local = managed_tree(index)
    edit(local, "c", result_metadata=None)
    local.state[eid("c")] = Managed(
        kind="card", hash=local_hash(local.entities[eid("c")])
    )
    return local


def test_removing_a_model_written_without_its_columns_archives_it():
    index = model_index()
    local = written_without_columns(index)
    del local.entities[eid("c")]

    plan = plan_for(index, local)

    assert not plan.conflicts
    assert [(op.action, op.entity_id) for op in plan.operations] == [
        ("archive", eid("c"))
    ]


def test_dropping_a_models_columns_from_its_file_is_not_a_conflict():
    index = model_index()
    local = managed_tree(index)
    edit(local, "c", result_metadata=None, description="edited in the files")

    plan = plan_for(index, local)

    assert not plan.conflicts
    assert [(op.action, op.changes) for op in plan.operations] == [
        ("update", ["description"])
    ]


def test_export_keeps_an_edit_to_a_model_written_without_its_columns(
    tmp_path, monkeypatch
):
    index = model_index()
    local = written_without_columns(index)
    edit(local, "c", description="edited in the files")
    yamlio.write(tmp_path / CONFIG, {})
    write_tree(tmp_path, render(list(local.entities.values())), local.state, {})
    monkeypatch.setattr(workflow, "connect", lambda *args: connected(index))

    result = workflow.export(offline_client(), tmp_path)

    assert not result.conflicts
    assert result.kept == ["collections/main/coll_a/card_c.yaml"]


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
    index.forget(CARD, 10)

    plan = plan_for(index, local)

    assert plan.empty
    assert [d.change for d in plan.drift] == ["deleted in Metabase"]


def test_an_item_moved_out_of_the_managed_collections_is_never_recreated():
    index = remote(collection=[collection(1, "a")], card=[card(10, "c", 1)])
    local = managed_tree(index)
    index.forget(CARD, 10)
    index.add_outside(CARD, 10, eid("c"))

    held = plan_for(index, local)
    forced = plan_for(index, local, force=True)

    assert [(c.entity_id, c.change) for c in held.conflicts] == [
        (eid("c"), "moved out of the managed collections in Metabase")
    ]
    assert held.empty and forced.empty


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


def test_references_that_exist_nowhere_are_refused_before_applying():
    index = remote(
        collection=[collection(1, "a")],
        dashboard=[
            {"id": 20, "entity_id": eid("b"), "name": "board", "collection_id": 1}
        ],
    )
    local = local_from(index, [(COLLECTION, 1), (DASHBOARD, 20)], {eid("a"), eid("b")})
    edit(
        local,
        "b",
        collection_id=eid("x"),
        dashcards=[
            Dashcard(
                entity_id=eid("d"), card_id=eid("y"), row=0, col=0, size_x=4, size_y=4
            )
        ],
    )

    with pytest.raises(ValidationFailed) as raised:
        build_plan(local, connected(index), scope={eid("a")})

    assert [p.split(": ", 1)[1] for p in raised.value.problems] == [
        f"collection_id refers to collection {eid('x')}, which doesn't exist in the "
        + "files or in Metabase",
        f"dashcards refers to card {eid('y')}, which doesn't exist in the files or "
        + "in Metabase",
    ]


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


class NothingCreatedYet(MetabaseClient):
    def __init__(self) -> None:
        super().__init__(
            Settings.model_validate({"url": "http://mb.test", "api_key": "k"})
        )
        self.requested: list[str] = []

    def get(self, path: str, **params: str) -> object:
        self.requested.append(path)
        return []


def test_recovery_lists_each_kind_once(tmp_path):
    journal = Journal(tmp_path)
    for tag in "pq":
        journal.record(
            Posting(
                op=f"{eid(tag)}:base:create",
                kind=CARD,
                entity_id=eid(tag),
                name=tag,
                container=None,
                existing=[],
            )
        )
    client = NothingCreatedYet()

    reconcile(client, v58, journal, LocalTree(root=tmp_path))

    assert client.requested == ["/api/card"]


def test_a_create_that_lands_after_recovery_is_imported_not_duplicated(tmp_path):
    index = remote(collection=[collection(1, "a")])
    local = local_from(index, [(COLLECTION, 1)], {eid("a")})
    local.root = tmp_path
    local.entities[eid("n")] = Entity(
        kind=CARD,
        data=Card(
            name="slow",
            entity_id=eid("n"),
            collection_id=eid("a"),
            display="table",
            dataset_query={},
            visualization_settings={},
        ),
        path=Path("slow.yaml"),
    )
    journal = Journal(tmp_path)
    op = f"{eid('n')}:base:create"
    journal.record(
        Started(op=op, action="create", kind=CARD, entity_id=eid("n"), name="slow")
    )
    journal.record(
        Posting(
            op=op, kind=CARD, entity_id=eid("n"), name="slow", container=1, existing=[]
        )
    )

    reconcile(NothingCreatedYet(), v58, journal, local)
    index.add(CARD, card(10, "c", 1, name="slow"))
    plan = plan_for(index, local)

    assert plan.renames == {eid("n"): eid("c")}
    assert "create" not in [op.action for op in plan.operations]


def test_recovery_does_not_take_over_items_it_was_removing(tmp_path):
    journal = Journal(tmp_path)
    journal.record(
        Started(
            op=f"{eid('c')}:base:archive",
            action="archive",
            kind=CARD,
            entity_id=eid("c"),
            name="stray",
        )
    )
    local = LocalTree(root=tmp_path)

    reconcile(offline_client(), v58, journal, local)

    assert eid("c") not in local.state


def test_fmt_does_not_count_as_a_first_sync(tmp_path):
    yamlio.write(tmp_path / CONFIG, {})
    yamlio.write(
        tmp_path / "collections" / "main" / "new.yaml",
        {
            "name": "New",
            "display": "table",
            "dataset_query": {
                "lib/type": "mbql/query",
                "database": "Warehouse",
                "stages": [{"lib/type": "mbql.stage/native", "native": "SELECT 1"}],
            },
            "visualization_settings": {},
            "serdes/meta": [{"model": "Card"}],
        },
    )

    assigned, _ = workflow.fmt(tmp_path)

    assert assigned
    assert not (tmp_path / STATE).exists()
