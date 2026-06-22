"""Pulse (dashboard subscription) integration — the only real-server proof of
the pulse serialize/apply path, which is otherwise mock-only."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from metabase_sync.apply import run as run_apply
from metabase_sync.client import MetabaseClient
from metabase_sync.export import run_export

from ._builders import unique_name
from ._mb import MetabaseAdmin

pytestmark = pytest.mark.integration


def test_pulse_round_trip(
    mb: MetabaseAdmin, metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    """Create a dashboard subscription with an email recipient, export, and
    confirm it lands on disk with the recipient stored by email and round-trips
    as a no-op."""
    coll = mb.create_collection(unique_name("pulse-coll"))
    card = mb.create_native_card(
        unique_name("pcard"), "SELECT 1", collection_id=coll["id"]
    )
    dash = mb.create_dashboard(unique_name("pulse-dash"), collection_id=coll["id"])
    detail = mb.set_dashboard_contents(
        dash["id"],
        [
            {
                "id": -1,
                "card_id": card["id"],
                "row": 0,
                "col": 0,
                "size_x": 12,
                "size_y": 4,
                "parameter_mappings": [],
                "visualization_settings": {},
                "series": [],
            }
        ],
    )
    dashcard_id = detail["dashcards"][0]["id"]
    admin = mb.admin_user()
    name = unique_name("weekly-pulse")

    try:
        mb.create_pulse(
            name,
            dashboard_id=dash["id"],
            collection_id=coll["id"],
            cards=[
                {
                    "id": card["id"],
                    "dashboard_card_id": dashcard_id,
                    "dashboard_id": dash["id"],
                    "include_csv": False,
                    "include_xls": False,
                }
            ],
            channels=[
                {
                    "channel_type": "email",
                    "schedule_type": "daily",
                    "schedule_hour": 8,
                    "enabled": True,
                    "recipients": [{"id": admin["id"], "email": admin["email"]}],
                }
            ],
        )
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"pulse creation unsupported on this instance: {e}")

    state = tmp_path / "state"
    with MetabaseClient(metabase_url, metabase_api_key) as client:
        run_export(client, state)

    pulse_file = next(
        (p for p in (state / "pulses").glob("*.yaml") if name in p.read_text()), None
    )
    assert pulse_file is not None, "pulse did not export to pulses/"
    doc = yaml.safe_load(pulse_file.read_text())
    emails = [r["email"] for ch in doc["channels"] for r in ch["recipients"]]
    assert admin["email"] in emails

    with MetabaseClient(metabase_url, metabase_api_key) as client:
        p = run_apply(client, state, mode="plan")
    for c in p.changes:
        if c.resource == "pulses" and c.name == name:
            assert c.action == "skip", f"pulse should round-trip: {c.summary}"
