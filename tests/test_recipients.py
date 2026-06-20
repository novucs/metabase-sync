"""Pulse recipients that don't match a user on the target instance must surface
loudly. A silent drop is the kind of bug that gets noticed only when an exec
stops getting their Monday email.

The pure payload builder reports the missing list; the caller (plan vs apply)
decides what to do with it (warn vs error)."""

from __future__ import annotations

from pathlib import Path

from metabase_sync.apply import _build_pulse_payload
from metabase_sync.diff import RemoteIndex


def _index_with_users(*emails: str) -> RemoteIndex:
    idx = RemoteIndex()
    for i, e in enumerate(emails, start=1):
        idx.users_by_email[e] = {"id": i, "email": e}
    return idx


def _doc(recipients_email: list[str]) -> dict:
    return {
        "name": "weekly",
        "cards": [],
        "channels": [
            {
                "channel_type": "email",
                "schedule_type": "weekly",
                "schedule_hour": 8,
                "schedule_day": "mon",
                "recipients": [{"email": e} for e in recipients_email],
            }
        ],
    }


def test_payload_excludes_missing_and_returns_their_list(tmp_path: Path):
    remote = _index_with_users("alice@example.com")
    doc = _doc(["alice@example.com", "bob@example.com"])

    payload, missing = _build_pulse_payload(
        doc, tmp_path / "p.yaml", None, None, {}, remote
    )
    assert payload["channels"][0]["recipients"] == [{"id": 1}]
    assert missing == [("weekly", "bob@example.com")]


def test_all_present_no_missing(tmp_path: Path):
    remote = _index_with_users("alice@example.com", "bob@example.com")
    doc = _doc(["alice@example.com", "bob@example.com"])

    payload, missing = _build_pulse_payload(
        doc, tmp_path / "p.yaml", None, None, {}, remote
    )
    ids = sorted(r["id"] for r in payload["channels"][0]["recipients"])
    assert ids == [1, 2]
    assert missing == []
