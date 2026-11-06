import logging
from types import ModuleType

from metabase_sync.errors import UnsupportedVersionError
from metabase_sync.format.api import v58

log = logging.getLogger(__name__)

VERSIONS: dict[int, ModuleType] = {58: v58, 64: v58, 65: v58}


def for_version(minor: int, tag: str) -> ModuleType:
    oldest = min(VERSIONS)
    if minor < oldest:
        raise UnsupportedVersionError(
            f"Metabase {tag} is older than 0.{oldest} (1.{oldest} for Enterprise)",
            hint="Use metabase-sync 0.3 for Metabase 0.55 to 0.57",
        )
    if minor not in VERSIONS:
        log.warning(
            "Metabase %s is untested; metabase-sync is tested on %s",
            tag,
            ", ".join(f"0.{v}" for v in VERSIONS),
        )
    return VERSIONS[max(v for v in VERSIONS if v <= minor)]
