<!-- Thanks for sending a PR! A few quick checks: -->

## What this changes

<!-- One paragraph: the change and why it's needed. -->

## How it was tested

- [ ] `uv run pytest tests/` (unit) is green
- [ ] `uv run pytest tests/integration -m integration` (integration) is green if the change touches apply/export/CLI
- [ ] `uv run ruff check src tests` is clean
- [ ] `uv run pyright src` is clean

## Backwards compatibility

<!-- Does this change the on-disk format? If yes, bump STATE_FORMAT_VERSION
     in src/metabase_sync/serialize/version.py and describe the migration. -->

## Documentation

- [ ] README updated if the user-visible behaviour changed
- [ ] `CHANGELOG.md` entry under `## [Unreleased]`
