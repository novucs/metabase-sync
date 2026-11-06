import contextlib
import os
import secrets
import stat
from pathlib import Path
from typing import Any

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        super().increase_indent(flow, False)


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _represent_str)


def dumps(obj: Any) -> str:
    return yaml.dump(
        obj,
        Dumper=_Dumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=float("inf"),
    )


def loads(text: str) -> Any:
    return yaml.load(text, Loader=_Loader)


def read(path: Path) -> Any:
    return loads(path.read_text(encoding="utf-8"))


def problem(error: yaml.YAMLError) -> str:
    if isinstance(error, yaml.MarkedYAMLError) and error.problem_mark:
        return f"invalid YAML at line {error.problem_mark.line + 1}: {error.problem}"
    return f"invalid YAML: {error}"


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write(path: Path, obj: Any) -> None:
    write_text_atomic(path, dumps(obj))
