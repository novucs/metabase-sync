"""Orchestrate one plan/apply pass.

`run()` is the single public entry point. It owns the order of operations
(collections → snippets → cards → dashboards → pulses) and the cross-cutting
preflights (database existence, card_path resolution, optimistic-concurrency).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from metabase_sync.client import MetabaseClient
from metabase_sync.diff import RemoteIndex, build_remote_index
from metabase_sync.errors import (
    ConcurrencyDriftError,
    MetabaseSyncError,
    PreflightError,
)
from metabase_sync.plan import Plan
from metabase_sync.serialize.dashboards import read_dashboard_files
from metabase_sync.serialize.databases import read_databases
from metabase_sync.serialize.pulses import read_pulses
from metabase_sync.serialize.version import check_stamp

from ._cards import apply_cards
from ._collections import apply_collections
from ._dashboards import apply_dashboards
from ._pulses import apply_pulses
from ._shared import ApplyContext, Mode
from ._snippets import apply_snippets

log = logging.getLogger(__name__)

# Versions we've actively tested round-trips against. Outside this band we
# either warn (newer than ceiling) or refuse (older than floor / known-broken).
_TESTED_FLOOR = (0, 55)  # v0.55 + actively maintained by Metabase
_TESTED_CEILING = (0, 62)  # last verified in integration CI
_HARD_FLOOR = (0, 45)  # collection API model is too different below this


class UnsupportedMetabaseVersion(MetabaseSyncError):
    """Refused to run against a Metabase version known to be incompatible."""


def run(
    client: MetabaseClient,
    state_dir: Path,
    *,
    mode: Mode,
    delete: bool = False,
    only: set[str] | None = None,
    force: bool = False,
    allow_missing_recipients: bool = False,
    concurrency_snapshot: dict[str, str] | None = None,
) -> Plan:
    plan = Plan()

    check_stamp(state_dir)
    _check_metabase_version(client)
    ref_problems = _reference_preflight(state_dir)
    remote = build_remote_index(client)
    db_problems = _database_preflight(state_dir, remote)
    if ref_problems or db_problems:
        _raise_combined_preflight(ref_problems, db_problems)

    if mode == "apply" and concurrency_snapshot and not force:
        _concurrency_check(concurrency_snapshot, remote)

    plan.concurrency_snapshot = remote.updated_at_snapshot()

    ctx = ApplyContext(
        client=client,
        state_dir=state_dir,
        remote=remote,
        plan=plan,
        mode=mode,
        db_id_by_name={n: int(d["id"]) for n, d in remote.databases_by_name.items()},
        allow_missing_recipients=allow_missing_recipients,
    )

    selected = only or {"collections", "snippets", "cards", "dashboards", "pulses"}
    if "collections" in selected:
        apply_collections(ctx)
    if "snippets" in selected:
        apply_snippets(ctx)
    if "cards" in selected:
        apply_cards(ctx)
    if "dashboards" in selected:
        apply_dashboards(ctx)
    if "pulses" in selected:
        apply_pulses(ctx)

    if delete:
        raise NotImplementedError("--delete is not yet wired")

    return plan


def _check_metabase_version(client: MetabaseClient) -> None:
    """Single GET on /api/session/properties to surface the server version.
    Warn if outside our tested band; refuse to run on versions so old the API
    shape we depend on doesn't exist."""
    try:
        props = client.get("/api/session/properties")
    except Exception as e:  # noqa: BLE001 — best-effort; never let this block a run
        log.warning("could not read Metabase version: %s", e)
        return
    if not isinstance(props, dict):
        return
    version = props.get("version")
    if not isinstance(version, dict):
        return
    raw = version.get("tag")
    if not isinstance(raw, str):
        return
    parts = _parse_version(raw)
    if parts is None:
        log.warning(
            "Metabase version tag %r not in major.minor form — skipping check", raw
        )
        return
    if parts < _HARD_FLOOR:
        raise UnsupportedMetabaseVersion(
            f"Metabase {raw} is older than the hard floor v{_HARD_FLOOR[0]}.{_HARD_FLOOR[1]}. "
            f"The collections API shape differs significantly on those versions and the "
            f"round-trip will lose data. Upgrade your Metabase to v{_TESTED_FLOOR[0]}.{_TESTED_FLOOR[1]} or later."
        )
    if parts < _TESTED_FLOOR:
        log.warning(
            "Metabase %s is older than our tested floor v%d.%d — proceed with caution.",
            raw,
            _TESTED_FLOOR[0],
            _TESTED_FLOOR[1],
        )
    elif parts > _TESTED_CEILING:
        log.warning(
            "Metabase %s is newer than our tested ceiling v%d.%d — surface any oddness "
            "as a github issue.",
            raw,
            _TESTED_CEILING[0],
            _TESTED_CEILING[1],
        )


_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)")


def _parse_version(tag: str) -> tuple[int, int] | None:
    """Parse e.g. 'v0.62.2', '0.62.2-RC1', 'v1.62-snapshot' → (0, 62) / (1, 62)."""
    m = _VERSION_RE.search(tag)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# --- preflights ---------------------------------------------------------------


def _reference_preflight(state_dir: Path) -> list[str]:
    """Walk every dashboard and pulse on disk; verify their card/dashboard refs
    point at real files. Returns the list of problem strings (empty = OK)."""
    problems: list[str] = []

    for directory, doc in read_dashboard_files(state_dir):
        rel = directory.relative_to(state_dir)
        for dc in doc.get("dashcards") or []:
            cp = dc.get("card_path")
            if cp is None:
                continue
            target = (directory / cp).resolve()
            if not target.exists():
                problems.append(
                    f"{rel}/dashboard.yaml → card_path '{cp}' (no such file)"
                )

    for path, doc in read_pulses(state_dir):
        rel = path.relative_to(state_dir)
        dp = doc.get("dashboard_path")
        if dp:
            target = (path.parent / dp).resolve()
            if not target.exists():
                problems.append(f"{rel} → dashboard_path '{dp}' (no such file)")
        for c in doc.get("cards") or []:
            cp = c.get("card_path")
            if cp is None:
                continue
            target = (path.parent / cp).resolve()
            if not target.exists():
                problems.append(f"{rel} → card_path '{cp}' (no such file)")

    return problems


def _database_preflight(state_dir: Path, remote: RemoteIndex) -> list[str]:
    """Verify every state/databases/*.yaml manifest has a matching DB by name
    + engine on the target. Returns problem strings."""
    problems: list[str] = []
    desired = read_databases(state_dir)
    for d in desired:
        name = d.get("name")
        if name not in remote.databases_by_name:
            problems.append(f"missing on target: {name}")
            continue
        actual_engine = remote.databases_by_name[name].get("engine")
        if d.get("engine") != actual_engine:
            problems.append(
                f"engine mismatch for '{name}': state={d.get('engine')} target={actual_engine}"
            )
    return problems


def _raise_combined_preflight(ref_problems: list[str], db_problems: list[str]) -> None:
    msg_parts: list[str] = ["preflight failed:"]
    if ref_problems:
        msg_parts.append(
            "  reference preflight — unresolved card_path / dashboard_path refs:"
        )
        msg_parts.extend(f"    {line}" for line in ref_problems)
    if db_problems:
        msg_parts.append("  database preflight:")
        msg_parts.extend(f"    {line}" for line in db_problems)
    msg_parts.append(
        "  (fix the issues above or re-export to refresh refs / create databases first)"
    )
    raise PreflightError("\n".join(msg_parts))


def _concurrency_check(snapshot: dict[str, str], remote: RemoteIndex) -> None:
    """Compare the snapshot taken at plan time with the current remote state.
    If any item changed (different `updated_at` or has been deleted), abort —
    the user must re-plan."""
    current = remote.updated_at_snapshot()
    drifted: list[tuple[str, str, str | None]] = []
    for key, planned in snapshot.items():
        observed = current.get(key)
        if observed is None or observed != planned:
            drifted.append((key, planned, observed))
    if drifted:
        lines = "\n".join(
            f"  {key}: was {planned!r}, now {observed!r}"
            for key, planned, observed in drifted
        )
        raise ConcurrencyDriftError(
            "concurrency check failed — remote items changed since plan:\n"
            f"{lines}\n"
            "  re-run `metabase-sync plan`, or pass --force to overwrite anyway."
        )
