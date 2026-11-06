# metabase-sync

[![PyPI](https://img.shields.io/pypi/v/metabase-sync.svg)](https://pypi.org/project/metabase-sync/)
[![Python](https://img.shields.io/pypi/pyversions/metabase-sync.svg)](https://pypi.org/project/metabase-sync/)
[![CI](https://github.com/novucs/metabase-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/novucs/metabase-sync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Manage Metabase as files. Export your collections, questions, dashboards, snippets, and subscriptions to YAML, edit them, and apply the changes back, seeing exactly what will change first. Works with the open-source edition.

```
$ metabase-sync plan
  ~ update card 'Orders by date'  collections/main/sales/orders_by_date.yaml
      --- metabase
      +++ collections/main/sales/orders_by_date.yaml
      @@ -8,7 +8,7 @@
             native: |-
      -        SELECT count(*)
      +        SELECT count(*) AS orders
               FROM orders
  + create dashboard 'Q4 review'  collections/main/sales/q4_review.yaml
  - archive card 'Old revenue chart'

Plan: 1 to create, 1 to update, 1 to archive.
```

## Install

```bash
uv tool install metabase-sync
```

Needs Python 3.14 or later and Metabase 0.58 or later. For Metabase 0.55 to 0.57, use metabase-sync 0.3.

## Quick start

Create an API key in Metabase (Admin settings > Authentication > API keys) in the Administrators group. Then:

```bash
export METABASE_URL=https://metabase.example.com
export METABASE_API_KEY=mb_...

mkdir metabase && cd metabase
metabase-sync init
metabase-sync export
```

To manage just one collection, use `metabase-sync init --collection "Finance"`.

## Usage

**Changed something in Metabase?** Pull it into the files:

```
$ metabase-sync export
Updated 2 files (1 added, 1 changed).
```

**Changed the files?** Apply them. `apply` shows what will change and asks first:

```
$ metabase-sync apply
  + create card 'Revenue by month'  collections/main/sales/revenue_by_month.yaml

Plan: 1 to create.

Apply these changes? [y/N]: y
  + create card 'Revenue by month'

Applied 1 change.
```

`metabase-sync plan` shows the same without applying, for reviews and CI.

A new file creates an item. Deleting a file archives the item, unless something that stays still uses it; then `plan` tells you what.

**Changed both?** metabase-sync keeps track of what each side changed since the last sync, in `metabase-sync.state`. Keep that file with the others.

| Your files | Metabase  | `export`             | `apply`             |
|------------|-----------|----------------------|---------------------|
| unchanged  | changed   | takes Metabase's     | leaves it alone     |
| changed    | unchanged | keeps yours          | applies yours       |
| changed    | changed   | stops                | stops               |

`--force` picks one side for everything: `export --force` takes Metabase's version, and `apply --force` takes yours.

`metabase-sync status` shows where things stand. `metabase-sync --help` lists every command and option.

## Configuration

Set these as environment variables, or in a `.env` file:

| Variable               | Default |                                                              |
|------------------------|---------|--------------------------------------------------------------|
| `METABASE_URL`         |         | Required.                                                    |
| `METABASE_API_KEY`     |         | Required, unless `METABASE_HEADERS` carries the credentials. |
| `METABASE_HEADERS`     |         | Extra headers as JSON, for example for an auth proxy.        |
| `METABASE_TIMEOUT`     | `120`   | Seconds per request.                                         |
| `METABASE_CONCURRENCY` | `12`    | Requests at once.                                            |

Optional settings go in `metabase-sync.yaml`:

| Setting       | Default   |                                                                                     |
|---------------|-----------|-------------------------------------------------------------------------------------|
| `collections` | all       | Entity ids of the collections to manage. `init --collection` fills this in.         |
| `snippets`    | see below | Whether to manage snippets. On when managing everything, off with `collections`.    |
| `removal`     | `archive` | What happens to an item whose file you delete: `archive` or `delete`.               |
| `unmanaged`   | `keep`    | What happens to items added in Metabase inside managed collections: `keep`, `archive`, or `delete`. Anything still in use is kept, and `plan` says why. |

For several Metabase instances, use one folder each and run with `--chdir`.

## CI

[`examples/`](examples/) has GitHub Actions workflows to plan on pull requests, apply on merge, and check for changes made in Metabase. Use `apply --auto-approve` in CI.

| Exit code | Meaning                                                      |
|-----------|--------------------------------------------------------------|
| 0         | Success, no changes                                          |
| 1         | Error, conflict, or apply declined                           |
| 2         | `plan --detailed-exitcode` or `export --check` found changes |
| 130       | Interrupted                                                  |

## Good to know

- `apply` never overwrites a change made in Metabase without `--force`.
- An interrupted `export` or `apply` is safe to run again.
- A plan saved with `plan --out` won't apply if anything changed since.
- Not managed: permissions, users, groups, alerts, segments, legacy metrics, public links, personal collections, and the questions inside documents.
- Upgrading from 0.3: export into a new folder and replace the old files.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Not affiliated with Metabase, Inc.
