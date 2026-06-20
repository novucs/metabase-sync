"""Helpers used by every `_<resource>.py` apply module.

`ApplyContext` is the single argument passed into each resource applier — it
carries the live client, parsed remote state, the in-progress plan, the
shared `{disk path → id}` maps, and the run-level flags. Adding a new flag
goes here, not in every per-resource signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from metabase_sync.client import MetabaseClient
from metabase_sync.diff import RemoteIndex
from metabase_sync.plan import Plan, diff_summary

Mode = Literal["plan", "apply"]


@dataclass
class ApplyContext:
    client: MetabaseClient
    state_dir: Path
    remote: RemoteIndex
    plan: Plan
    mode: Mode
    db_id_by_name: dict[str, int] = field(default_factory=dict)
    collection_id_by_disk_path: dict[Path, int] = field(default_factory=dict)
    card_id_by_disk_path: dict[Path, int] = field(default_factory=dict)
    dashboard_id_by_disk_path: dict[Path, int] = field(default_factory=dict)
    allow_missing_recipients: bool = False


# --- diff helpers ----------------------------------------------------------------


def diff_fields(
    desired: dict[str, Any], remote: dict[str, Any], keys: tuple[str, ...]
) -> list[tuple[str, Any, Any]]:
    """Per-field diff. Each tuple is (field, before, after)."""
    out: list[tuple[str, Any, Any]] = []
    for k in keys:
        if desired.get(k) != remote.get(k):
            out.append((k, remote.get(k), desired.get(k)))
    return out


def summarize_diffs(diffs: list[tuple[str, Any, Any]]) -> str:
    return "; ".join(
        diff_summary(field, before, after) for field, before, after in diffs
    )


def find_by_name(by_id: dict[int, dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in by_id.values():
        if item.get("name") == name:
            return item
    return None


def resolve_card_path(
    card_path_str: str | None, from_dir: Path, card_id_by_disk_path: dict[Path, int]
) -> int | None:
    """Resolve a `card_path` reference (relative to `from_dir`) to a numeric
    card id via the map populated during the cards pass."""
    if not card_path_str:
        return None
    target = (from_dir / card_path_str).resolve()
    return card_id_by_disk_path.get(target)


# --- dataset_query normalisation (used by cards diff) ---------------------------


_VOLATILE_KEYS = frozenset({"info", "lib/uuid", "lib.convert/converted?"})


def normalize_dataset_query(dq: dict[str, Any]) -> dict[str, Any]:
    """Strip volatile/server-only keys so MBQL5 cards diff cleanly.

    Metabase regenerates `lib/uuid` on every save and adds `info` cross-refs
    server-side, neither of which represent user-meaningful changes.
    """
    return _strip_volatile(dq)


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _strip_volatile(v)
            for k, v in sorted(obj.items())
            if k not in _VOLATILE_KEYS
        }
    if isinstance(obj, list):
        return [_strip_volatile(x) for x in obj]
    return obj


def normalize(obj: Any) -> Any:
    """Deep-sort dict keys + walk lists. Used for shape comparison."""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [normalize(x) for x in obj]
    return obj
