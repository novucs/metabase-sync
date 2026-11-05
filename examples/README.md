# Examples

GitHub Actions workflows to copy into your own repository.

| File | Does |
| --- | --- |
| [`github-actions-plan-on-pr.yml`](github-actions-plan-on-pr.yml) | Comments `metabase-sync plan` on every pull request that touches the files. |
| [`github-actions-apply-on-merge.yml`](github-actions-apply-on-merge.yml) | Runs `metabase-sync apply --auto-approve` on merge to `main`, behind a GitHub environment so it can require approval. |
| [`github-actions-drift-detection.yml`](github-actions-drift-detection.yml) | Exports nightly and opens a pull request if anyone changed Metabase in the UI. |

They expect:

- The files in `metabase/`. Adjust `paths:` and `working-directory:` if yours live elsewhere.
- `METABASE_URL` and `METABASE_API_KEY` as repository secrets.

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
