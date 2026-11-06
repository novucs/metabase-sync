# Examples

GitHub Actions workflows to copy into your own repository.

| File | Does |
| --- | --- |
| [`github-actions-plan-on-pr.yml`](github-actions-plan-on-pr.yml) | Comments `metabase-sync plan` on every pull request that touches the files, including when it fails. |
| [`github-actions-apply-on-merge.yml`](github-actions-apply-on-merge.yml) | Runs `metabase-sync apply --auto-approve` on merge to `main`, behind a GitHub environment so it can require approval, then commits `metabase-sync.state` and new entity ids back to `main`. |
| [`github-actions-drift-detection.yml`](github-actions-drift-detection.yml) | Exports nightly and opens a pull request if anyone changed Metabase in the UI. |

They expect:

- The files in `metabase/`. Adjust `paths:` and `working-directory:` if yours live elsewhere.
- `METABASE_URL` and `METABASE_API_KEY` as repository secrets, and `DRIFT_PR_TOKEN` (a token that can open pull requests) for drift detection.
- The metabase-sync version pinned in `uv tool install`. Raise it when you upgrade.

Commit `metabase-sync.state` after every apply. The apply workflow pushes it to `main` for you; if branch protection blocks that push, run the workflow with a token or bot that is allowed to push.

```
your-repo/
  metabase/
    metabase-sync.yaml
    metabase-sync.state
    .gitignore
    collections/
  .github/
    workflows/
      metabase-plan.yml
      metabase-apply.yml
```
