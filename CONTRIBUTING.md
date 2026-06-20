# Contributing

Thanks for taking a look. PRs welcome; small issues / discussions also welcome.

## Dev setup

```bash
git clone https://github.com/novucs/metabase-sync
cd metabase-sync
uv sync --all-groups
```

`uv` is the only required tool; install it from [astral.sh/uv](https://docs.astral.sh/uv/).

## Running the tests

```bash
# Unit tests (fast, no network)
uv run pytest tests/ --ignore=tests/integration

# Integration tests (spin up a real Metabase via docker compose, ~60-90s per test)
uv run pytest tests/integration -m integration
```

The integration suite requires Docker and uses [`tests/integration/docker-compose.yml`](tests/integration/docker-compose.yml) to start a throwaway Metabase + Postgres. The session fixture mints an admin API key automatically.

## Running the CLI against a local Metabase

```bash
docker compose -f tests/integration/docker-compose.yml up -d
# Wait ~60s for Metabase to boot, then visit http://localhost:3000 and finish setup.
export METABASE_URL=http://localhost:3000
export METABASE_API_KEY=mb_...   # minted from the admin UI
uv run python -m metabase_sync export
```

## Pull request flow

1. Open a PR with a focused change. Add or update a test if you can.
2. Keep `pyproject.toml`'s `requires-python` honest — don't introduce features from a newer Python without bumping it.
3. Update `CHANGELOG.md` under `## [Unreleased]`.
4. CI must be green.

## Releases (maintainers)

1. Bump `version` in `pyproject.toml` and `__version__` in `src/metabase_sync/__init__.py`.
2. Move the `## [Unreleased]` section in `CHANGELOG.md` to a new `## [X.Y.Z] — YYYY-MM-DD` heading.
3. Commit, then `git tag vX.Y.Z && git push && git push --tags`.
4. The [`publish.yml`](.github/workflows/publish.yml) workflow runs `uv build` and publishes to PyPI via Trusted Publishing.

### One-time setup before the first release

- Create the GitHub repo at `github.com/novucs/metabase-sync`.
- On [PyPI](https://pypi.org/manage/account/publishing/), add a *pending publisher*:
  - Owner: `novucs`
  - Repository: `metabase-sync`
  - Workflow: `publish.yml`
  - Environment: `pypi`
- Confirm the tag triggers the workflow and the package lands on PyPI.

## Code conventions

- Type hints everywhere (`mypy` clean is nice-to-have, not required).
- Prefer `pathlib.Path` over strings for filesystem paths.
- Pydantic models in `models.py` use `extra="allow"` so unknown server fields don't break exports.
- All filesystem writes go through `serialize/yamlio.py` so byte-faithful round-trips stay verified by the existing tests.

## What's out of scope

These were considered and deferred. PRs are welcome but please open an issue first.

- Permissions / users / groups (Metabase models them with a graph; non-trivial).
- Soft-delete via `apply --delete` (the stub exists; real implementation is a follow-up).
- A `diff` command that prints full SQL unified diffs (would be nice; defer until requested).
