# metabase-sync

[![PyPI](https://img.shields.io/pypi/v/metabase-sync.svg)](https://pypi.org/project/metabase-sync/)
[![Python](https://img.shields.io/pypi/pyversions/metabase-sync.svg)](https://pypi.org/project/metabase-sync/)
[![CI](https://github.com/novucs/metabase-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/novucs/metabase-sync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Metabase as code for the open-source edition. Export collections, questions, models, dashboards, snippets, and dashboard subscriptions to YAML in the [metabase/representations](https://github.com/metabase/representations) format, review changes in git, and apply them back with a Terraform-style `plan` and `apply`.

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

Tested on Metabase 0.58 (LTS), 0.64 (LTS), and 0.65, with Python 3.14 and 3.15. Versions in between, and newer ones, work with a warning. For Metabase 0.55 to 0.57, use metabase-sync 0.3.

## Quickstart

```bash
export METABASE_URL=https://metabase.example.com
export METABASE_API_KEY=mb_...
mkdir metabase && cd metabase
metabase-sync init
metabase-sync export
git add . && git commit -m "export metabase"
```

The API key must belong to the Administrators group (Admin settings > Authentication > API keys).

Day to day, build in Metabase and export, or edit files and apply:

```bash
metabase-sync status
metabase-sync export
metabase-sync plan
metabase-sync apply
```

## Commands

| Command        | Does                                                                                                                                                                               |
|----------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `init`         | Create `metabase-sync.yaml` and a `.gitignore`. `--collection NAME` manages only that collection, looked up by name or entity id; repeat it for more.                              |
| `export`       | Pull Metabase into the files, keeping edits you haven't applied. `--check` writes nothing and exits 2 if the files are out of date. `--force` takes Metabase's version everywhere. |
| `status`       | Show interrupted runs, changes to apply, changes made in Metabase, conflicts, and problems.                                                                                        |
| `plan`         | Show what `apply` would change, as a diff. `--out FILE` saves the plan, `--json` prints it as JSON, `--detailed-exitcode` exits 2 when there are changes to apply.                 |
| `apply [FILE]` | Apply the changes, after confirmation. With a saved plan, refuses if Metabase or the files changed since. `--auto-approve` skips the prompt.                                       |
| `validate`     | Check the files offline.                                                                                                                                                           |
| `fmt`          | Give files entity ids, and rewrite every file in canonical form and at its canonical path.                                                                                         |

Commands run in the current directory; `--chdir DIR` (or `METABASE_SYNC_CHDIR`) runs them in another. `--verbose` lists every file and HTTP request, and `--debug` shows tracebacks. These three options go before or after the command.

## Exit codes

| Code | Meaning                                                      |
|------|--------------------------------------------------------------|
| 0    | Success, no changes                                          |
| 1    | Error, conflict, or apply declined                           |
| 2    | `plan --detailed-exitcode` or `export --check` found changes |
| 130  | Interrupted                                                  |

## Configuration

Environment variables, or a `.env` file in the directory the command runs in:

| Variable               | Default |                                                              |
|------------------------|---------|--------------------------------------------------------------|
| `METABASE_URL`         |         | Required.                                                    |
| `METABASE_API_KEY`     |         | Required unless `METABASE_HEADERS` carries the credentials.  |
| `METABASE_HEADERS`     | `{}`    | JSON object of extra headers, for example for an auth proxy. |
| `METABASE_TIMEOUT`     | `120`   | Seconds per request.                                         |
| `METABASE_CONCURRENCY` | `12`    | Parallel requests.                                           |

`metabase-sync.yaml`:

```yaml
collections:
  - HyocLue0f_iGHD90nRWrl
snippets: false
removal: archive
unmanaged: keep
```

- `collections` limits the files to those collections and everything inside them. Leave it out to manage the whole instance. `metabase-sync init --collection NAME` looks up a collection's entity id, and prints the line to add when the file already exists.
- `snippets` decides whether the files hold the instance's snippets. It defaults to `true` without `collections` and `false` with it, so repos that each manage their own collections leave the shared snippets alone. Questions can use snippets either way.
- `removal` is `archive` (the default) or `delete`. It decides what `apply` does with an item whose file you removed.
- `unmanaged` is `keep` (the default), `archive`, or `delete`. It decides what `apply` does with items in Metabase that were never in the files, such as a question created in Metabase since the last export. See [Removing items](#removing-items).

## Changes on both sides

`metabase-sync.state` records each item's content at the last sync, so the tool can tell your edits from edits made in Metabase. Commit it, and the entity ids `apply` writes into the files, after every `apply` and `export`. The [apply workflow](examples/github-actions-apply-on-merge.yml) commits them back for you.

| Your files | Metabase  | `export`                          | `plan` and `apply`            |
|------------|-----------|-----------------------------------|-------------------------------|
| unchanged  | changed   | takes Metabase's version          | leave it alone and list it    |
| changed    | unchanged | keeps your file                   | apply your change             |
| changed    | changed   | stops; `--force` takes Metabase's | stop; `--force` applies yours |

`apply --force` applies your files over every change made in Metabase since the last sync, not only the conflicts. `export --force` overwrites your unapplied edits, so commit them first.

To keep both sides of a conflict, commit your files, run `metabase-sync export --force`, redo your edits from `git diff`, then `apply`.

## What isn't managed

- Questions inside documents. `plan` lists them as unmanaged.
- Snippet folders, a paid-plan feature. Snippets are written without their folder.
- Public links.
- Personal collections.

Official collections are kept, but CI tests only the open-source edition.

## Files

```
metabase-sync.yaml
metabase-sync.state
collections/
  main/
    sales.yaml
    sales/
      orders_by_date.yaml
      q4_review.yaml
      q4_review/
        dashboard_question.yaml
  snippets/
    date_filter.yaml
subscriptions/
  q4_review.yaml
```

- Each item is one YAML file named after it. Collections and dashboards have a folder of the same name for what they contain.
- Collections, questions, dashboards, and snippets pass Metabase's own representation validator. Subscriptions are not part of that format and live in `subscriptions/`.
- References between items use entity ids. Databases, tables, and fields are referenced by name, so the files apply to any instance with the same databases.
- SQL with several lines is stored as a literal block, exactly as written.
- Same-named siblings get a short id suffix, so paths never change because of an unrelated item.
- With `unmanaged: keep` (the default), `apply` only removes items listed in `metabase-sync.state`, so content created in Metabase since the last export is never touched.
- A subscription takes its name and its cards from its dashboard, as Metabase does. `attachments` lists the dashcards sent as CSV or XLSX, by dashcard `entity_id`, with Metabase's `include_csv`, `include_xls`, `format_rows`, and `pivot_results` settings.

## Writing new items by hand

Create the file without an `entity_id`, then `plan` and `apply`:

```yaml
name: Orders by date
collection_id: HyocLue0f_iGHD90nRWrl
display: line
dataset_query:
  database: Sample Database
  lib/type: mbql/query
  stages:
    - lib/type: mbql.stage/native
      native: |-
        SELECT created_at::date AS day, count(*)
        FROM orders
        GROUP BY 1
visualization_settings: {}
serdes/meta:
  - model: Card
```

Write the fields Metabase's schemas require, such as a card's `visualization_settings` and a dashcard's `row` and `col`. `validate` names any that are missing or misspelt. `apply` writes the id Metabase assigns into the file. To reference a new item from another file before applying, run `metabase-sync fmt` first to give it an id.

## Importing existing items

A file for an item that exists in Metabase but not in `metabase-sync.state` plans as an import: `apply` records the item and applies any differences, instead of creating a duplicate. Files are matched by entity id, or without one by name and collection (a subscription by dashboard and channels). A file that matches several items, or that matches the same item as another file, stops as a conflict; set its `entity_id` to import one, or pass `--force` to create it.

## Removing items

Delete the file. `plan` shows an archive (or a delete with `removal: delete`). Removing a collection archives it once; Metabase archives its contents with it, and `plan` lists them. A dashboard takes its own questions and its subscriptions with it. Remove a question's dashcards along with its file.

A removal never breaks what stays. If anything that stays uses the item or sits inside it, such as a dashboard showing the question or a document in the collection, `plan` refuses the removal, even with `--force`. The one exception is items that only exist in Metabase with `unmanaged: keep`: `plan` stops until you export them, or `--force` removes the item anyway.

With `removal: delete`, each removed item gets its own delete. Deleting a collection or dashboard in Metabase also deletes what it holds, archived items included, so `plan` refuses a delete that would destroy anything the plan doesn't delete.

With `unmanaged: archive` or `unmanaged: delete`, `apply` also removes items that only exist in Metabase but sit inside a collection or dashboard the files manage, such as a question someone added to a managed collection. The same rule applies: anything that stays keeps what it uses and what holds it, and `plan` lists those items with the reason. Everything outside the managed collections is left alone, so a new repo, a wider `collections:`, or another Metabase instance removes nothing until you export. Top-level items count as managed only in a repo without `collections:` whose files hold a top-level question or dashboard. metabase-sync can't see what other repos, other users' personal collections, or links inside documents use; with `unmanaged: delete`, items that only exist in Metabase are deleted permanently.

## Safety

- `apply PLANFILE` applies a saved plan only if planning now gives the same result. If Metabase or the files changed since `plan --out`, it stops before writing anything.
- `apply` records each change and each new id in `.metabase-sync/journal.jsonl` before relying on it. If a run is interrupted, the next `apply` finds what was created and finishes the rest.
- If Metabase refuses a change, `apply` records the changes it made and stops. Fix the files and run it again.
- If Metabase doesn't confirm a create, `apply` stops instead of sending it again. The next run checks for the item before creating it.
- `export` replaces each file atomically and removes stale files last, so an interrupted export loses nothing. Run it again to finish.

## Upgrading from 0.3

Export into a new directory with `metabase-sync init --chdir new` and `metabase-sync export --chdir new`, then replace the old files.

## CI

[`examples/`](examples/) has GitHub Actions workflows for planning on pull requests, applying on merge, and nightly drift detection.

## Several instances

Keep one directory per instance in the same repository, each with its own `metabase-sync.yaml` and `metabase-sync.state`, and point each run at one with `--chdir` and that instance's `METABASE_URL` and `METABASE_API_KEY`.

## Out of scope

Permissions, users, groups, alerts, segments, legacy metrics, and personal collections.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. This is an unofficial tool, not affiliated with Metabase, Inc.
