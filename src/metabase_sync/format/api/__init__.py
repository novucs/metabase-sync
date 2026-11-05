from types import ModuleType

from metabase_sync.format.api import v58

VERSIONS: dict[int, ModuleType] = {58: v58, 64: v58, 65: v58}


def for_version(minor: int) -> ModuleType:
    return VERSIONS[max((v for v in VERSIONS if v <= minor), default=min(VERSIONS))]
