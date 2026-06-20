# Examples

Templates you can copy into your own repo to wire Metabase changes through your normal git workflow.

## GitHub Actions

| File | What it does |
| --- | --- |
| [`github-actions-plan-on-pr.yml`](github-actions-plan-on-pr.yml) | On every PR that touches your state tree, run `metabase-sync plan` and post the output as a PR comment. |
| [`github-actions-apply-on-merge.yml`](github-actions-apply-on-merge.yml) | On merge to `main`, run `metabase-sync apply`. Gated by a GitHub environment so approval can be required. |
| [`github-actions-drift-detection.yml`](github-actions-drift-detection.yml) | Nightly `export` against the live instance — if anyone edited via the UI, opens a PR with the diff. |

Both expect:

- The state tree at `infrastructure/metabase/state/` (adjust the `paths:` filter if yours lives elsewhere).
- `METABASE_URL` and `METABASE_API_KEY` set as GitHub Secrets.
- A `pypi`-published version of `metabase-sync` (these install with `uv tool install`).

## Layout suggestion for the consuming repo

```
your-repo/
├── infrastructure/
│   └── metabase/
│       ├── .env.example        # METABASE_URL=... / METABASE_API_KEY=
│       ├── .gitignore          # .env, .plan.json, .last-apply.json
│       └── state/              # ← managed by metabase-sync
└── .github/
    └── workflows/
        ├── metabase-plan.yml   # copy of github-actions-plan-on-pr.yml
        └── metabase-apply.yml  # copy of github-actions-apply-on-merge.yml
```

Run `metabase-sync` from inside `infrastructure/metabase/`. The `state/` directory is the only thing you commit.
