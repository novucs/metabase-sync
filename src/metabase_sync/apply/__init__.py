"""`metabase-sync apply` orchestration.

Public entry point is `run()`. The rest of this package is internal: each
`_<resource>.py` module contains a single `apply_<resource>(ctx)` function
that mutates the plan and (in apply mode) calls the Metabase API.
"""

from metabase_sync.diff import build_remote_index

from ._pulses import _build_pulse_payload  # re-exported for unit tests
from ._runner import (
    _concurrency_check,
    _database_preflight,
    _reference_preflight,
    run,
)
from ._shared import ApplyContext
from ._shared import resolve_card_path as _resolve_card_path

__all__ = [
    "ApplyContext",
    "run",
    "build_remote_index",
    # Internals re-exported for test access only — not part of the supported API.
    "_build_pulse_payload",
    "_concurrency_check",
    "_database_preflight",
    "_reference_preflight",
    "_resolve_card_path",
]
