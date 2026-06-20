"""Plan data structures shared by `plan` and `apply`.

A Plan is what `plan` shows the user and what `apply` executes. Both commands
run the same orchestration in `apply.py`; the only difference is whether
mutating HTTP calls are skipped (plan) or made (apply).

Each Change carries enough detail to print a human report and to be saved to
`state/.plan.json` for audit / CI consumption.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Action = Literal["create", "update", "skip", "archive"]
Resource = Literal["collections", "snippets", "cards", "dashboards", "pulses"]


@dataclass
class Change:
    resource: Resource
    action: Action
    relpath: str
    name: str
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    # {"<resource>:<entity_id>": updated_at} captured at plan-time; consumed by
    # apply for the optimistic-concurrency check. String-keyed so the same
    # structure round-trips through JSON (tuple keys would not).
    concurrency_snapshot: dict[str, str] = field(default_factory=dict)

    def add(self, change: Change) -> None:
        self.changes.append(change)

    def by_resource(self) -> dict[str, list[Change]]:
        groups: dict[str, list[Change]] = {}
        for c in self.changes:
            groups.setdefault(c.resource, []).append(c)
        return groups

    def counts(self) -> dict[str, dict[str, int]]:
        groups = self.by_resource()
        out: dict[str, dict[str, int]] = {}
        for resource, items in groups.items():
            counts = {"create": 0, "update": 0, "skip": 0, "archive": 0}
            for c in items:
                counts[c.action] += 1
            out[resource] = counts
        return out

    def total_writes(self) -> int:
        return sum(1 for c in self.changes if c.action != "skip")

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": [asdict(c) for c in self.changes],
            "counts": self.counts(),
            "concurrency_snapshot": dict(self.concurrency_snapshot),
        }


_RESOURCE_ORDER = ("collections", "snippets", "cards", "dashboards", "pulses")
_ACTION_LABELS = {"create": "CREATE", "update": "UPDATE", "archive": "ARCHIVE"}


def render_report(plan: Plan, state_dir: Path) -> str:
    """Human-readable per-item summary for the terminal."""
    counts = plan.counts()
    groups = plan.by_resource()
    lines: list[str] = [f"plan ← {state_dir}", ""]
    totals = {"create": 0, "update": 0, "skip": 0, "archive": 0}

    for resource in _RESOURCE_ORDER:
        if resource not in counts:
            continue
        c = counts[resource]
        for k, v in c.items():
            totals[k] += v
        lines.append(
            f"{resource + ':':<13}{c['create']} create, {c['update']} update, "
            f"{c['skip']} skip" + (f", {c['archive']} archive" if c["archive"] else "")
        )
        for change in groups[resource]:
            if change.action == "skip":
                continue
            label = _ACTION_LABELS[change.action]
            path_col = f"{change.relpath:<55.55}"
            tail = change.summary or change.name
            lines.append(f"  {label:<7} {path_col}  {tail}")

    lines.append("")
    suffix = f", {totals['archive']} archive" if totals["archive"] else ""
    lines.append(
        f"{totals['create']} create, {totals['update']} update{suffix}, "
        f"{totals['skip']} skip"
    )
    if plan.total_writes() == 0:
        lines.append("nothing to do.")
    else:
        lines.append("run `metabase-sync apply` to apply this plan.")
    return "\n".join(lines)


def diff_summary(field: str, before: Any, after: Any) -> str:
    """One-line 'field: before → after' (or 'field: summary' when after is None).

    The two-arg variant is for cases where a literal old→new pair isn't useful
    (e.g. SQL bodies — we don't want to dump multi-line SQL into a stdout line;
    we render '15 → 18 lines, +3 −2' as a single summary in the before slot).
    """
    if after is None:
        return f"{field}: {_inline(before)}"
    return f"{field}: {_short(before)} → {_short(after)}"


def _short(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        s = value.replace("\n", "\\n")
        return f"'{s[:40]}'" + ("…" if len(value) > 40 else "")
    return repr(value)[:50]


def _inline(value: Any) -> str:
    if isinstance(value, str):
        return value
    return repr(value)
