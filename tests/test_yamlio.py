"""Frontmatter round-trip MUST preserve body bytes exactly — Metabase keeps
whatever whitespace the user typed and an off-by-one newline produces 200 false
'updates' per apply run.
"""

from pathlib import Path

import pytest

from metabase_sync.serialize.yamlio import (
    read_frontmatter_sql,
    write_frontmatter_sql,
)


@pytest.mark.parametrize(
    "body",
    [
        "SELECT 1",
        "SELECT 1\n",
        "SELECT 1\n\n\n",
        "   \nSELECT 1\n  ",
        "WITH cte AS (\n  SELECT * FROM t\n)\nSELECT *\nFROM cte\n",
        "no trailing newline at all",
        "",
    ],
    ids=lambda x: repr(x)[:30],
)
def test_frontmatter_body_preserves_bytes(tmp_path: Path, body: str):
    path = tmp_path / "card.sql"
    fm = {"entity_id": "abc", "name": "Test", "template_tags": {}}
    write_frontmatter_sql(path, fm, body)
    got_fm, got_body = read_frontmatter_sql(path)
    assert got_fm == fm
    assert got_body == body


def test_frontmatter_handles_dashes_in_body(tmp_path: Path):
    """A SQL body containing '---' lines must not confuse the parser."""
    path = tmp_path / "card.sql"
    body = "SELECT '---' AS sep\nFROM t\n---\nUNION ALL SELECT 'x', 'y'"
    write_frontmatter_sql(path, {"name": "edgey"}, body)
    _, got = read_frontmatter_sql(path)
    assert got == body


def test_atomic_write_preserves_existing_on_failure(tmp_path: Path, monkeypatch):
    """If the write hits an error after creating the tempfile, the original
    file must be intact and no .tmp turds left behind."""
    from metabase_sync.serialize import yamlio

    path = tmp_path / "card.sql"
    write_frontmatter_sql(path, {"name": "v1"}, "SELECT 1")
    original = path.read_text()

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(yamlio.os, "replace", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        write_frontmatter_sql(path, {"name": "v2"}, "SELECT 2")

    assert path.read_text() == original
    # No tmp turds left behind.
    leftovers = list(tmp_path.glob(".*.tmp")) + list(tmp_path.glob("*.tmp"))
    assert leftovers == []
