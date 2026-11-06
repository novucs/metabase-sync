from difflib import get_close_matches
from typing import TYPE_CHECKING, Any, get_args

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from metabase_sync.sync.operations import Drift


def count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def describe(
    error: ValidationError,
    model: type[BaseModel] | None = None,
    env_prefix: str = "",
) -> list[str]:
    problems = []
    for e in error.errors():
        where = ".".join(str(p) for p in e["loc"])
        if env_prefix and where:
            where = env_prefix + where.upper()
        message = (
            str(e.get("ctx", {}).get("error", e["msg"]))
            if e["type"] == "value_error"
            else e["msg"]
        )
        if e["type"] == "extra_forbidden" and model is not None:
            close = get_close_matches(str(e["loc"][-1]), _fields_at(model, e["loc"]))
            message = "unknown field" + (f"; did you mean {close[0]}?" if close else "")
        if not where:
            problems.append(message)
        elif e["type"] == "missing" and env_prefix:
            problems.append(f"{where} is not set")
        else:
            problems.append(f"{where}: {message}")
    return problems


def _fields_at(model: type[BaseModel], loc: tuple[str | int, ...]) -> list[str]:
    current: type[BaseModel] | None = model
    for part in loc[:-1]:
        if isinstance(part, str) and current is not None:
            info = current.model_fields.get(part)
            current = _model_in(info.annotation) if info else None
    return list(current.model_fields) if current else []


def _model_in(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return next(filter(None, map(_model_in, get_args(annotation))), None)


class MetabaseSyncError(Exception):
    hint: str | None = None

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class ConfigError(MetabaseSyncError):
    pass


class ApiError(MetabaseSyncError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} returned {status}: {body}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class UnsupportedVersionError(MetabaseSyncError):
    pass


class StateError(MetabaseSyncError):
    pass


class ValidationFailed(MetabaseSyncError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{count(len(problems), 'problem')} in the files")
        self.problems = problems


class StalePlanError(MetabaseSyncError):
    pass


class ConflictError(MetabaseSyncError):
    def __init__(self, conflicts: list[Drift], drifted: int = 0) -> None:
        also = f", including {count(drifted, 'other item')}" if drifted else ""
        super().__init__(
            f"{count(len(conflicts), 'conflict')} with Metabase",
            hint="Run `metabase-sync export --force` to take Metabase's versions "
            "(commit your edits first to keep them), or pass --force to overwrite "
            f"every change made in Metabase{also}",
        )
        self.conflicts = conflicts


class BlockedError(MetabaseSyncError):
    def __init__(self, blocked: list[Drift]) -> None:
        super().__init__(
            f"{count(len(blocked), 'removal')} can't be applied, even with --force",
            hint="Each line names what still needs the item: restore its file, or "
            "first change that, in the files or in Metabase",
        )
        self.blocked = blocked


class UnresolvedReferenceError(MetabaseSyncError):
    pass


class NotKeptError(MetabaseSyncError):
    def __init__(self, items: list[Drift]) -> None:
        names = "; ".join(f"{d.kind} {d.name!r} ({d.change})" for d in items)
        super().__init__(
            f"Metabase didn't keep all of the changes to {names}",
            hint="Metabase drops values it doesn't accept; fix those fields in the "
            "files, or run `metabase-sync export` to take what Metabase kept",
        )
        self.items = items


class ApplyStopped(MetabaseSyncError):
    def __init__(self, error: MetabaseSyncError, applied: int) -> None:
        super().__init__(
            f"Apply stopped partway, after {count(applied, 'change')}: {error}",
            hint=error.hint or "Fix the files, then run `metabase-sync apply` again",
        )
