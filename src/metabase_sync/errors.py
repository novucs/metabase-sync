"""Custom exception hierarchy.

These wrap the situations where we'd previously call `raise SystemExit("...")`.
The CLI catches `MetabaseSyncError` at the top level and renders a clean
message + exit code; tests can catch by type rather than parsing a string.
"""

from __future__ import annotations


class MetabaseSyncError(Exception):
    """Base class for every error this package raises deliberately."""


class PreflightError(MetabaseSyncError):
    """One or more preflights (reference, database) found problems before apply
    could begin. The message lists every problem; not just the first."""


class ConcurrencyDriftError(MetabaseSyncError):
    """A remote item changed since `plan` was computed. The user must re-plan,
    or pass `--force` to overwrite anyway."""


class MissingRecipientError(MetabaseSyncError):
    """A pulse references a recipient email that doesn't exist on the target.
    Pass `--allow-missing-recipients` to silently drop them."""


class ReferenceResolutionError(MetabaseSyncError):
    """A dashcard's `card_path` doesn't resolve to a card on disk during
    apply. Only fires if the reference preflight is bypassed or new files
    were deleted between preflight and the apply pass."""
