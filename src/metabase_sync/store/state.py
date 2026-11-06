from dataclasses import dataclass
from pathlib import Path

from metabase_sync.errors import StateError
from metabase_sync.store.yamlio import write_text_atomic

STATE = "metabase-sync.state"
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


@dataclass
class Managed:
    kind: str
    hash: str | None = None


def read_state(root: Path) -> dict[str, Managed]:
    path = root / STATE
    state: dict[str, Managed] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        if line.startswith(CONFLICT_MARKERS):
            raise StateError(
                f"{STATE} has git merge conflict markers",
                hint="Keep either side of the conflict, then run `metabase-sync export`",
            )
        parts = line.split()
        if len(parts) >= 2:
            state[parts[1]] = Managed(parts[0], parts[2] if len(parts) > 2 else None)
    return state


def _state_text(state: dict[str, Managed]) -> str:
    lines = sorted(
        " ".join(part for part in (entry.kind, eid, entry.hash) if part)
        for eid, entry in state.items()
    )
    return "\n".join(lines) + ("\n" if lines else "")


def write_state(root: Path, state: dict[str, Managed]) -> None:
    write_text_atomic(root / STATE, _state_text(state))
