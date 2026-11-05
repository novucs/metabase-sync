from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from metabase_sync.sync.operations import Drift


def describe(
    error: ValidationError,
    env_prefix: str = "",
    place: Callable[[tuple[str | int, ...]], str] | None = None,
) -> list[str]:
    problems = []
    for e in error.errors():
        where = place(e["loc"]) if place else ".".join(str(p) for p in e["loc"])
        if env_prefix and where:
            where = env_prefix + where.upper()
        message = (
            str(e.get("ctx", {}).get("error", e["msg"]))
            if e["type"] == "value_error"
            else e["msg"]
        )
        if not where:
            problems.append(message)
        elif e["type"] == "missing" and env_prefix:
            problems.append(f"{where} is not set")
        else:
            problems.append(f"{where}: {message}")
    return problems


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
        super().__init__(f"{len(problems)} problem(s) in the files")
        self.problems = problems


class StalePlanError(MetabaseSyncError):
    pass


class ConflictError(MetabaseSyncError):
    def __init__(self, conflicts: list[Drift]) -> None:
        items = (
            "1 item conflicts"
            if len(conflicts) == 1
            else f"{len(conflicts)} items conflict"
        )
        super().__init__(
            f"{items} with Metabase",
            hint="Run `metabase-sync export` to take Metabase's versions (commit your "
            "edits first to keep them), or pass --force to overwrite Metabase's",
        )
        self.conflicts = conflicts


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
