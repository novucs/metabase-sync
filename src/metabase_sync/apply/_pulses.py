"""Apply pulses (dashboard subscriptions).

Recipient resolution is intentionally split from the payload build so the
caller decides what to do with missing recipients: warn during plan, error
during apply unless `--allow-missing-recipients` is set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from metabase_sync.diff import RemoteIndex
from metabase_sync.plan import Change
from metabase_sync.serialize.canon import PULSE, PULSE_CHILDREN, canonical
from metabase_sync.serialize.pulses import read_pulses
from metabase_sync.serialize.yamlio import write_yaml

from ._shared import (
    ApplyContext,
    diff_container_fields,
    diff_fields,
    resolve_card_path,
    summarize_diffs,
)

log = logging.getLogger(__name__)


def apply_pulses(ctx: ApplyContext) -> None:
    for path, doc in read_pulses(ctx.state_dir):
        relpath = str(path.relative_to(ctx.state_dir))
        eid = doc.get("entity_id")

        collection_slug = doc.get("collection_slug")
        collection_id = None
        if collection_slug:
            collection_dir = (ctx.state_dir / "collections" / collection_slug).resolve()
            collection_id = ctx.collection_id_by_disk_path.get(collection_dir)

        dashboard_path_str = doc.get("dashboard_path")
        dashboard_id = None
        if dashboard_path_str:
            dashboard_yaml = (path.parent / dashboard_path_str).resolve()
            dashboard_id = ctx.dashboard_id_by_disk_path.get(dashboard_yaml.parent)

        remote_pulse = (
            ctx.remote.pulses_by_entity.get(eid)
            if eid
            else _find_by_dashboard_and_name(ctx.remote, dashboard_id, doc["name"])
        )

        desired, missing_recipients = _build_pulse_payload(
            doc, path, collection_id, dashboard_id, ctx.card_id_by_disk_path, ctx.remote
        )

        if missing_recipients:
            if ctx.mode == "plan":
                log.warning(
                    "pulse %s references %d recipient(s) with no matching user "
                    "on the target — apply will error unless "
                    "--allow-missing-recipients is passed.",
                    relpath,
                    len(missing_recipients),
                )
            elif ctx.mode == "apply" and not ctx.allow_missing_recipients:
                lines = "\n".join(
                    f"  pulse '{name}': {email}" for name, email in missing_recipients
                )
                raise SystemExit(
                    "pulse recipient(s) have no matching user on the target instance:\n"
                    f"{lines}\n"
                    "  rerun with --allow-missing-recipients to silently drop them, "
                    "or create the user(s) on the target first."
                )

        if remote_pulse is None:
            ctx.plan.add(
                Change(
                    resource="pulses",
                    action="create",
                    relpath=relpath,
                    name=doc["name"],
                    summary=f"{doc['name']} → {len(desired.get('channels') or [])} channel(s)",
                )
            )
            if ctx.mode == "apply":
                created = ctx.client.post("/api/pulse", desired)
                doc["entity_id"] = created.get("entity_id")
                write_yaml(path, canonical(doc, PULSE, PULSE_CHILDREN))
            continue

        diffs = _pulse_diffs(desired, remote_pulse)
        if diffs:
            ctx.plan.add(
                Change(
                    resource="pulses",
                    action="update",
                    relpath=relpath,
                    name=doc["name"],
                    summary=summarize_diffs(diffs),
                    details={"changes": diffs},
                )
            )
            if ctx.mode == "apply":
                ctx.client.put(f"/api/pulse/{remote_pulse['id']}", desired)
        else:
            ctx.plan.add(
                Change(
                    resource="pulses", action="skip", relpath=relpath, name=doc["name"]
                )
            )


def _pulse_diffs(
    desired: dict[str, Any], remote: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    diffs = diff_fields(
        desired,
        remote,
        ("name", "collection_id", "dashboard_id", "skip_if_empty", "archived"),
    )
    # Metabase <= v0.55 returns disable_links as null; never diff the default
    # against it.
    if remote.get("disable_links") is not None and desired.get(
        "disable_links"
    ) != remote.get("disable_links"):
        diffs.append(
            ("disable_links", remote.get("disable_links"), desired.get("disable_links"))
        )
    diffs.extend(diff_container_fields(desired, remote, ("parameters",)))

    desired_cards = [_card_projection(c) for c in desired.get("cards") or []]
    remote_cards = [_card_projection(c) for c in remote.get("cards") or []]
    if desired_cards != remote_cards:
        diffs.append(("cards", f"{len(remote_cards)} → {len(desired_cards)}", None))

    desired_channels = [_channel_projection(c) for c in desired.get("channels") or []]
    remote_channels = [_channel_projection(c) for c in remote.get("channels") or []]
    if desired_channels != remote_channels:
        diffs.append(
            ("channels", f"{len(remote_channels)} → {len(desired_channels)}", None)
        )
    return diffs


def _card_projection(card: dict[str, Any]) -> tuple:
    return (
        card.get("id"),
        card.get("display"),
        card.get("include_csv") or False,
        card.get("include_xls") or False,
        card.get("format_rows", True),
        card.get("pivot_results") or False,
        card.get("parameter_mappings") or None,
    )


def _channel_projection(channel: dict[str, Any]) -> tuple:
    return (
        channel.get("channel_type"),
        channel.get("schedule_type"),
        channel.get("schedule_hour"),
        channel.get("schedule_day"),
        channel.get("schedule_frame"),
        channel.get("enabled", True),
        channel.get("channel_id"),
        sorted(r.get("id") for r in (channel.get("recipients") or []) if r.get("id")),
    )


def _find_by_dashboard_and_name(
    remote: RemoteIndex, dashboard_id: int | None, name: str
) -> dict[str, Any] | None:
    for p in remote.pulses_by_id.values():
        if p.get("dashboard_id") == dashboard_id and p.get("name") == name:
            return p
    return None


def _build_pulse_payload(
    doc: dict[str, Any],
    pulse_path: Path,
    collection_id: int | None,
    dashboard_id: int | None,
    card_id_by_disk_path: dict[Path, int],
    remote: RemoteIndex,
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Build the pulse PUT payload + return (pulse_name, email) pairs whose
    user wasn't found on the target. Caller decides what to do: warn during
    plan, raise during apply."""
    cards: list[dict[str, Any]] = []
    for c in doc.get("cards", []) or []:
        cid = resolve_card_path(
            c.get("card_path"), pulse_path.parent, card_id_by_disk_path
        )
        cards.append(
            {
                "id": cid,
                "display": c.get("display"),
                "include_csv": c.get("include_csv", False),
                "include_xls": c.get("include_xls", False),
                "format_rows": c.get("format_rows", True),
                "pivot_results": c.get("pivot_results", False),
                "parameter_mappings": c.get("parameter_mappings"),
            }
        )
    missing: list[tuple[str, str]] = []
    channels = []
    for ch in doc.get("channels", []) or []:
        ch_recipients: list[dict[str, Any]] = []
        for r in ch.get("recipients", []) or []:
            email = r.get("email")
            if not email:
                continue
            user = remote.users_by_email.get(email)
            if user is None:
                missing.append((doc["name"], email))
                continue
            ch_recipients.append({"id": user["id"]})
        channels.append(
            {
                "channel_type": ch["channel_type"],
                "schedule_type": ch["schedule_type"],
                "schedule_hour": ch.get("schedule_hour"),
                "schedule_day": ch.get("schedule_day"),
                "schedule_frame": ch.get("schedule_frame"),
                "enabled": ch.get("enabled", True),
                "channel_id": ch.get("channel_id"),
                "recipients": ch_recipients,
            }
        )
    payload = {
        "name": doc["name"],
        "collection_id": collection_id,
        "dashboard_id": dashboard_id,
        "skip_if_empty": doc.get("skip_if_empty", False),
        # `or False`: state exported from v0.55 stores an explicit null.
        "disable_links": doc.get("disable_links") or False,
        "archived": doc.get("archived") or False,
        "parameters": doc.get("parameters", []),
        "cards": cards,
        "channels": channels,
    }
    return payload, missing
