# Contributing

## Setup

```bash
git clone https://github.com/novucs/metabase-sync
cd metabase-sync
uv sync --all-groups
```

## Checks

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright src
uv run pytest tests --ignore=tests/integration --ignore=tests/contracts
```

## Contract tests

`tests/contracts` checks the models in `src/metabase_sync/format` against Metabase's published schemas, so it needs network access:

- The models in `format/representations` cover every field in `@metabase/representations` (pinned in `tests/pins.py`), apart from the fields in `LEFT_OUT`, each with a reason.
- Every request from each tested version's package in `format/api` is accepted by that version's published API (`https://www.metabase.com/docs/v0.NN/api.json`).

```bash
uv run pytest tests/contracts
```

## Integration tests

The integration suite runs on four pytest-xdist workers. Each worker starts its own Metabase and Postgres with Docker Compose, creates an admin API key, and tears them down afterwards. Allow about 7 GB of memory for Docker.

The suite also checks exported files with Metabase's own validator, `npx @metabase/representations validate-schema`, so it needs Node.js, and checks that its content sets every field the files manage.

```bash
uv run pytest tests/integration
METABASE_VERSION=v0.58.39 uv run pytest tests/integration
```

CI runs the suite on each version in the `integration` matrix of `.github/workflows/ci.yml`, which must match `TESTED` in `src/metabase_sync/metabase/client.py`. The default for local runs is in `tests/integration/docker-compose.yml`. Workers use ports 3000 to 3003; set `METABASE_PORT` to move the range. Pass `-n 0` to run on a single worker. To reuse a running instance, set `METABASE_INTEGRATION_SKIP_COMPOSE=1` with `METABASE_URL` and `METABASE_API_KEY`, and pass `-n 0`.

## New Metabase versions

The Canary workflow runs the integration suite against `metabase/metabase:latest` every Monday. When Metabase releases a version:

1. Add its image tag to the `integration` matrix in `ci.yml`, its major version to `TESTED` in `client.py`, and to `VERSIONS` in `format/api/__init__.py`, pointing at the newest package.
2. Fix what the contract and integration tests report. A new field in the files is managed in `format/representations` or added to `LEFT_OUT`. When the API changed, add a package such as `format/api/v66` holding a full copy of each changed resource module, importing the unchanged ones from the previous package, and point the version at it.
3. Release.

When a version reaches end of life on [Metabase's support page](https://www.metabase.com/version-support), remove it from the matrix, `TESTED`, and `VERSIONS`, and delete any package no version uses.

## Pull requests

1. Keep the change focused and add a test that fails without it.
2. Add an entry under `## [Unreleased]` in `CHANGELOG.md`.
3. CI must pass.

## Releases

1. Bump `version` in `pyproject.toml` and `__version__` in `src/metabase_sync/__init__.py`.
2. Move `## [Unreleased]` in `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading.
3. Tag `vX.Y.Z` and push the tag; [`publish.yml`](.github/workflows/publish.yml) publishes to PyPI.
