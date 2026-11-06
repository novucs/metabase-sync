"""Remote items with no state file are archived (Terraform-style destroy):
listed per item, in dependency order, never touching personal/system
collections, and refused outright when state/ is empty."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from metabase_sync.apply._archive import apply_archive, unmanaged_collections
from metabase_sync.apply._shared import ApplyContext
from metabase_sync.diff import RemoteIndex
from metabase_sync.errors import PreflightError
from metabase_sync.plan import Plan

ALL = {"collections", "snippets", "cards", "dashboards", "pulses"}


def _col(i: int, location: str = "/", **kw) -> dict:
    return {"id": i, "name": f"c{i}", "location": location, **kw}


def _remote(**by_res: list[dict]) -> RemoteIndex:
    idx = RemoteIndex()
    for res, items in by_res.items():
        getattr(idx, f"{res}_by_id").update({it["id"]: it for it in items})
    return idx


def _ctx(tmp_path: Path, remote: RemoteIndex, mode: str = "plan") -> ApplyContext:
    (tmp_path / "collections" / "keep").mkdir(parents=True)
    (tmp_path / "collections" / "keep" / "_collection.yaml").write_text("name: keep\n")
    return ApplyContext(
        client=MagicMock(), state_dir=tmp_path, remote=remote, plan=Plan(), mode=mode
    )


def _archived(ctx: ApplyContext) -> list[tuple[str, int]]:
    return [
        (c.resource, c.details["id"]) for c in ctx.plan.changes if c.action == "archive"
    ]


def test_unclaimed_items_planned_claimed_kept(tmp_path: Path):
    ctx = _ctx(
        tmp_path,
        _remote(
            collections=[_col(1), _col(2)],
            cards=[{"id": 10, "collection_id": 1}, {"id": 11, "collection_id": 1}],
        ),
    )
    ctx.claimed |= {("collections", 1), ("cards", 10)}
    apply_archive(ctx, ALL)
    assert _archived(ctx) == [("cards", 11), ("collections", 2)]
    ctx.client.put.assert_not_called()  # plan mode


def test_every_item_counted_children_before_parents(tmp_path: Path):
    ctx = _ctx(
        tmp_path,
        _remote(
            collections=[_col(1), _col(2, "/1/"), _col(3, "/1/2/")],
            cards=[{"id": 10, "collection_id": 3}],
            dashboards=[{"id": 20, "collection_id": 2}],
            pulses=[{"id": 30, "collection_id": None, "dashboard_id": 20}],
        ),
    )
    apply_archive(ctx, ALL)
    assert _archived(ctx) == [
        ("pulses", 30),
        ("dashboards", 20),
        ("cards", 10),
        ("collections", 3),
        ("collections", 2),
        ("collections", 1),
    ]
    assert ctx.plan.summary() == "Plan: 0 to add, 0 to change, 6 to destroy."


def test_apply_puts_archived_in_order_skipping_dashboard_questions(tmp_path: Path):
    ctx = _ctx(
        tmp_path,
        _remote(
            collections=[_col(1)],
            cards=[
                {"id": 10, "collection_id": 1, "dashboard_id": 20},
                {"id": 11, "collection_id": 1},
            ],
            dashboards=[{"id": 20, "collection_id": 1}],
        ),
        mode="apply",
    )
    apply_archive(ctx, ALL)
    paths = [c.args[0] for c in ctx.client.put.call_args_list]
    assert paths == ["/api/dashboard/20", "/api/card/11", "/api/collection/1"]
    assert all(c.args[1] == {"archived": True} for c in ctx.client.put.call_args_list)
    assert len(_archived(ctx)) == 4  # dashboard question still counted


def test_personal_system_and_unlisted_collections_untouched(tmp_path: Path):
    cols = [
        _col(1, personal_owner_id=5),
        _col(2, "/1/"),
        _col(3, type="trash"),
        _col(4, type="instance-analytics"),
        _col(6, is_sample=True),
    ]
    ctx = _ctx(
        tmp_path,
        _remote(
            collections=cols,
            cards=[
                {"id": 10, "collection_id": 2},
                {"id": 11, "collection_id": 99},  # another user's personal collection
                {"id": 12, "collection_id": None},  # root: managed
            ],
            dashboards=[{"id": 20, "collection_id": 1}],
            pulses=[{"id": 30, "collection_id": None, "dashboard_id": 20}],
        ),
    )
    assert unmanaged_collections(ctx.remote.collections_by_id) == {1, 2, 3, 4, 6}
    apply_archive(ctx, ALL)
    assert _archived(ctx) == [("cards", 12)]


def test_only_limits_archive(tmp_path: Path):
    ctx = _ctx(
        tmp_path,
        _remote(collections=[_col(1)], cards=[{"id": 10, "collection_id": 1}]),
    )
    apply_archive(ctx, {"cards"})
    assert _archived(ctx) == [("cards", 10)]


def test_empty_state_refuses(tmp_path: Path):
    ctx = ApplyContext(
        client=MagicMock(),
        state_dir=tmp_path / "wrong",
        remote=_remote(collections=[_col(1)]),
        plan=Plan(),
        mode="apply",
    )
    with pytest.raises(PreflightError, match="refusing to archive 1"):
        apply_archive(ctx, ALL)
    ctx.client.put.assert_not_called()


def test_empty_state_fine_when_nothing_to_archive(tmp_path: Path):
    ctx = ApplyContext(
        client=MagicMock(),
        state_dir=tmp_path / "wrong",
        remote=RemoteIndex(),
        plan=Plan(),
        mode="apply",
    )
    apply_archive(ctx, ALL)
    assert ctx.plan.changes == []


def test_report_reads_like_terraform(tmp_path: Path):
    from metabase_sync.plan import Change, render_report

    ctx = _ctx(
        tmp_path,
        _remote(
            collections=[_col(1), _col(2, "/1/")],
            cards=[{"id": 10, "name": "Old", "collection_id": 2}],
            snippets=[{"id": 40, "name": "snip"}],
        ),
    )
    ctx.claimed |= {("collections", 1), ("collections", 2)}
    ctx.plan.add(Change(resource="cards", action="skip", relpath="k", name="k"))
    apply_archive(ctx, ALL)
    out = render_report(ctx.plan, tmp_path)
    assert "skip" not in out and "collections:" not in out
    assert "cards:       1 to destroy\n" in out
    assert "ARCHIVE c1/c2/Old" in out and "card 10" in out
    assert "ARCHIVE Snippets/snip" in out
    assert "Plan: 0 to add, 0 to change, 2 to destroy." in out


def test_apply_summary():
    from metabase_sync.plan import Change

    p = Plan()
    p.add(Change(resource="cards", action="create", relpath="a", name="a"))
    p.add(Change(resource="cards", action="update", relpath="b", name="b"))
    p.add(Change(resource="cards", action="archive", relpath="cards/1", name="c"))
    assert (
        p.summary(applied=True)
        == "Apply complete! Resources: 1 added, 1 changed, 1 destroyed."
    )
