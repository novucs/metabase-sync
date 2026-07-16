"""The pulse diff must cover the whole payload: schedules, recipients, cards
and flags previously planned as skips no matter what changed on disk."""

from __future__ import annotations

from typing import Any

from metabase_sync.apply._pulses import _pulse_diffs


def _desired(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "p",
        "collection_id": None,
        "dashboard_id": 7,
        "skip_if_empty": False,
        "disable_links": False,
        "archived": False,
        "parameters": [],
        "cards": [
            {
                "id": 5,
                "display": "bar",
                "include_csv": False,
                "include_xls": False,
                "format_rows": True,
                "pivot_results": False,
                "parameter_mappings": None,
            }
        ],
        "channels": [
            {
                "channel_type": "email",
                "schedule_type": "daily",
                "schedule_hour": 8,
                "schedule_day": None,
                "schedule_frame": None,
                "enabled": True,
                "channel_id": None,
                "recipients": [{"id": 3}],
            }
        ],
    }
    base.update(overrides)
    return base


def _remote(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "p",
        "collection_id": None,
        "dashboard_id": 7,
        "skip_if_empty": False,
        "disable_links": False,
        "archived": False,
        "parameters": None,
        "cards": [
            {
                "id": 5,
                "name": "remote extra key",
                "collection_id": 9,
                "display": "bar",
                "include_csv": False,
                "include_xls": False,
                "format_rows": True,
                "pivot_results": False,
                "parameter_mappings": [],
            }
        ],
        "channels": [
            {
                "channel_type": "email",
                "schedule_type": "daily",
                "schedule_hour": 8,
                "schedule_day": None,
                "schedule_frame": None,
                "enabled": True,
                "channel_id": None,
                "recipients": [{"id": 3, "email": "someone@example.com"}],
            }
        ],
    }
    base.update(overrides)
    return base


def test_identical_pulse_no_diff():
    assert _pulse_diffs(_desired(), _remote()) == []


def test_schedule_change_detected():
    desired = _desired()
    desired["channels"][0]["schedule_hour"] = 9
    assert [f for f, _b, _a in _pulse_diffs(desired, _remote())] == ["channels"]


def test_recipient_change_detected():
    desired = _desired()
    desired["channels"][0]["recipients"] = [{"id": 3}, {"id": 4}]
    assert [f for f, _b, _a in _pulse_diffs(desired, _remote())] == ["channels"]


def test_card_options_change_detected():
    desired = _desired()
    desired["cards"][0]["include_csv"] = True
    assert [f for f, _b, _a in _pulse_diffs(desired, _remote())] == ["cards"]


def test_flags_and_parameters_detected():
    desired = _desired(disable_links=True, archived=True, parameters=[{"id": "p1"}])
    fields = {f for f, _b, _a in _pulse_diffs(desired, _remote())}
    assert {"disable_links", "archived", "parameters"} <= fields


def test_null_remote_disable_links_does_not_diff():
    # Metabase v0.55 returns disable_links as null; the default must not
    # plan an update forever against it.
    assert _pulse_diffs(_desired(), _remote(disable_links=None)) == []
