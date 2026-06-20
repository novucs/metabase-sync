"""On-disk format versioning.

`state/.metabase-sync-version` is a one-line text file containing an integer.
Bump `STATE_FORMAT_VERSION` whenever the on-disk layout changes in a way that
an older binary can't read (or could read but would produce a corrupt
round-trip). Apply refuses to run against a state tree written by a NEWER
binary than ours.
"""

from __future__ import annotations

from pathlib import Path

from metabase_sync.errors import MetabaseSyncError

STATE_FORMAT_VERSION = 1
_STAMP_FILENAME = ".metabase-sync-version"


class StateFormatError(MetabaseSyncError):
    """The state tree was written by a different version of this tool."""


def write_stamp(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / _STAMP_FILENAME).write_text(f"{STATE_FORMAT_VERSION}\n")


def read_stamp(state_dir: Path) -> int | None:
    path = state_dir / _STAMP_FILENAME
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def check_stamp(state_dir: Path) -> None:
    """Refuse to operate on a state tree written by a newer binary than ours."""
    found = read_stamp(state_dir)
    if found is None:
        # Pre-stamp state trees (or empty dirs) are accepted — they were
        # written before stamping existed. Apply will write a stamp on the
        # next export.
        return
    if found > STATE_FORMAT_VERSION:
        raise StateFormatError(
            f"state tree at {state_dir} was written by metabase-sync with "
            f"on-disk format v{found}, but this binary only supports v{STATE_FORMAT_VERSION}. "
            f"Upgrade metabase-sync or check out the matching version of your state repo."
        )
    if found < STATE_FORMAT_VERSION:
        # Older format we still support — re-export to refresh.
        # For now, no migrations exist; future versions add them here.
        return
