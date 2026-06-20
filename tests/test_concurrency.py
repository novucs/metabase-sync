"""Optimistic-concurrency check: between plan and apply, if a remote item's
`updated_at` shifts (someone edited via the UI), apply must refuse to overwrite
unless --force is passed."""

from __future__ import annotations

import pytest

from metabase_sync.apply import _concurrency_check
from metabase_sync.diff import RemoteIndex
from metabase_sync.errors import ConcurrencyDriftError


def _index_with_card(entity_id: str, updated_at: str) -> RemoteIndex:
    idx = RemoteIndex()
    item = {"id": 1, "entity_id": entity_id, "updated_at": updated_at}
    idx.cards_by_entity[entity_id] = item
    idx.cards_by_id[1] = item
    return idx


def test_no_drift_passes():
    snapshot = {"cards:ENT1": "2026-06-20T10:00:00Z"}
    remote = _index_with_card("ENT1", "2026-06-20T10:00:00Z")
    # Should not raise.
    _concurrency_check(snapshot, remote)


def test_drift_aborts():
    snapshot = {"cards:ENT1": "2026-06-20T10:00:00Z"}
    remote = _index_with_card("ENT1", "2026-06-20T10:05:00Z")  # changed
    with pytest.raises(ConcurrencyDriftError) as exc:
        _concurrency_check(snapshot, remote)
    msg = str(exc.value)
    assert "ENT1" in msg
    assert "--force" in msg


def test_remote_deleted_aborts():
    """An item that existed at plan time but is gone now also counts as drift."""
    snapshot = {"cards:ENT_GONE": "2026-06-20T10:00:00Z"}
    remote = RemoteIndex()  # nothing
    with pytest.raises(ConcurrencyDriftError) as exc:
        _concurrency_check(snapshot, remote)
    assert "ENT_GONE" in str(exc.value)


def test_unsnapshotted_items_are_ignored():
    """The check only verifies items captured at plan time. New remote items
    (added between plan and apply) don't cause a spurious abort."""
    snapshot: dict[str, str] = {}
    remote = _index_with_card("ENT_NEW", "2026-06-20T10:00:00Z")
    _concurrency_check(snapshot, remote)  # no raise


def test_plan_round_trips_through_json():
    """The whole Plan (including the concurrency snapshot) must serialise to
    JSON — the .last-apply.json audit log depends on it."""
    import json

    from metabase_sync.plan import Change, Plan

    p = Plan()
    p.add(Change(resource="cards", action="create", relpath="x.sql", name="x"))
    p.concurrency_snapshot["cards:ENT1"] = "2026-06-20T10:00:00Z"

    serialized = json.dumps(p.to_dict())
    payload = json.loads(serialized)
    assert payload["concurrency_snapshot"] == {"cards:ENT1": "2026-06-20T10:00:00Z"}
    assert payload["changes"][0]["name"] == "x"
