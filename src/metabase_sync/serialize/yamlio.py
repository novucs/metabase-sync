"""YAML I/O helpers shared by all serializers.

Goals:
* Deterministic output (sort_keys=False, dict insertion order, block style).
* Atomic writes: tempfile + os.replace so Ctrl+C or a crashing process can never
  leave a half-written YAML on disk.
* Frontmatter format for SQL bodies: a YAML block delimited by '---' lines,
  followed by the raw SQL body.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

_BLOCK_OPTS: dict[str, Any] = {
    "default_flow_style": False,
    "sort_keys": False,
    "allow_unicode": True,
    "width": 120,
}


def dump_yaml(obj: Any) -> str:
    return yaml.safe_dump(obj, **_BLOCK_OPTS)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write content to path atomically.

    The tempfile lives in the same directory as the target so os.replace is a
    rename within one filesystem (atomic on POSIX and Windows for files). On
    failure between write and replace, the original file (if any) is untouched
    and the tempfile is cleaned up.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # delete=False so we can rename it; we clean up on failure ourselves.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_yaml(path: Path, obj: Any) -> None:
    _atomic_write_text(path, dump_yaml(obj))


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_BODY_SEPARATOR = "\n---body---\n"


def write_frontmatter_sql(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    """Write a SQL file with YAML frontmatter, preserving body bytes exactly.

    A literal `---body---` line separates frontmatter from body so we can store
    bodies whose own content contains `---\\n` lines without ambiguity, and so
    we never have to strip or pad trailing whitespace (which Metabase preserves
    verbatim).
    """
    content = "".join(["---\n", dump_yaml(frontmatter), "---", _BODY_SEPARATOR, body])
    _atomic_write_text(path, content)


def read_frontmatter_sql(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter")
    rest = text[4:]
    end = rest.find("---" + _BODY_SEPARATOR)
    if end < 0:
        raise ValueError(f"{path}: missing body separator")
    frontmatter = yaml.safe_load(rest[:end]) or {}
    body = rest[end + len("---") + len(_BODY_SEPARATOR) :]
    return frontmatter, body
