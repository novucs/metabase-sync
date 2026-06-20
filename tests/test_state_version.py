"""Schema-version stamp: `state/.metabase-sync-version` records the on-disk
format version. Apply refuses to run against a tree written by a NEWER binary."""

from __future__ import annotations

from pathlib import Path

import pytest

from metabase_sync.errors import MetabaseSyncError
from metabase_sync.serialize.version import (
    STATE_FORMAT_VERSION,
    StateFormatError,
    check_stamp,
    read_stamp,
    write_stamp,
)


def test_round_trip(tmp_path: Path):
    write_stamp(tmp_path)
    assert read_stamp(tmp_path) == STATE_FORMAT_VERSION


def test_no_stamp_is_accepted(tmp_path: Path):
    # Pre-stamp trees (or empty dirs) don't raise — apply will write a stamp.
    check_stamp(tmp_path)


def test_older_stamp_is_accepted(tmp_path: Path):
    if STATE_FORMAT_VERSION <= 1:
        pytest.skip("no older version exists yet")
    (tmp_path / ".metabase-sync-version").write_text("1\n")
    check_stamp(tmp_path)


def test_newer_stamp_aborts(tmp_path: Path):
    (tmp_path / ".metabase-sync-version").write_text(f"{STATE_FORMAT_VERSION + 1}\n")
    with pytest.raises(StateFormatError, match="newer") as exc:
        check_stamp(tmp_path)
    # Subclass of the base error so the CLI catches it cleanly.
    assert isinstance(exc.value, MetabaseSyncError)


def test_corrupt_stamp_treated_as_absent(tmp_path: Path):
    (tmp_path / ".metabase-sync-version").write_text("not-a-number\n")
    # Doesn't raise — corrupt stamp falls through to "no stamp" semantics.
    check_stamp(tmp_path)
