"""CLI: `metabase-sync export | plan | apply | index`.

The intended workflow is:

    metabase-sync export        # pull current Metabase state into state/
    edit files...               # author or modify dashboards/cards in your editor
    metabase-sync plan          # see what would change, per item
    metabase-sync apply         # execute the same diff

Exit codes follow the terraform convention:

    0 = success and no changes (or apply finished cleanly)
    1 = error (HTTP failure, validation failure, preflight failure)
    2 = `plan` detected pending changes (informational; not an error)
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.logging import RichHandler

from metabase_sync import __version__
from metabase_sync.errors import MetabaseSyncError
from metabase_sync.settings import load_settings


def _configure_logging(verbose: bool) -> None:
    """Configure the package logger to print to stderr via Rich. Verbose mode
    drops to DEBUG and surfaces every retry/probe."""
    handler = RichHandler(
        show_time=False,
        show_path=False,
        markup=False,
        rich_tracebacks=False,
        log_time_format="",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    pkg_logger = logging.getLogger("metabase_sync")
    pkg_logger.handlers.clear()
    pkg_logger.addHandler(handler)
    pkg_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    pkg_logger.propagate = False


app = typer.Typer(add_completion=False, no_args_is_help=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"metabase-sync {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Show DEBUG-level logs (per-HTTP-request and retry diagnostics).",
    ),
) -> None:
    """Metabase as code: export, plan, and apply via the Metabase REST API."""
    _configure_logging(verbose=verbose)
    _ = version


def _client_from_settings(s):
    from metabase_sync.client import MetabaseClient

    return MetabaseClient(
        s.metabase_url,
        s.metabase_api_key,
        timeout_s=s.http_timeout_s,
        max_retries=s.http_max_retries,
        retry_backoff_s=s.http_retry_backoff_s,
    )


def _parse_only(only: Optional[str]) -> Optional[set[str]]:
    return {x.strip() for x in only.split(",")} if only else None


def _is_interactive() -> bool:
    """True if we should prompt the user. Honour CI=true / non-tty / --yes."""
    if os.environ.get("CI", "").lower() == "true":
        return False
    return sys.stdout.isatty() and sys.stdin.isatty()


@app.command()
def export(
    state_dir: Optional[Path] = typer.Option(None, "--state-dir", "-s"),
) -> None:
    """Pull everything from Metabase into the on-disk state tree."""
    from metabase_sync.export import run_export

    s = load_settings(state_dir=state_dir)
    with _client_from_settings(s) as client:
        run_export(client, s.state_dir)


@app.command()
def plan(
    state_dir: Optional[Path] = typer.Option(None, "--state-dir", "-s"),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated subset of: collections,snippets,cards,dashboards,pulses",
    ),
) -> None:
    """Compute the diff between state/ and the live instance. Does not write.

    Exits 0 if there is nothing to do, 2 if changes are pending (so CI can fan
    out from a 2-exit), 1 on error.
    """
    from metabase_sync.apply import run
    from metabase_sync.plan import render_report

    s = load_settings(state_dir=state_dir)
    with _client_from_settings(s) as client:
        p = run(client, s.state_dir, mode="plan", only=_parse_only(only))

    typer.echo(render_report(p, s.state_dir))
    plan_path = s.state_dir / ".plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps(p.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    if p.total_writes() > 0:
        typer.echo(f"\nplan written to {plan_path}")
        raise typer.Exit(code=2)


@app.command()
def apply(
    state_dir: Optional[Path] = typer.Option(None, "--state-dir", "-s"),
    delete: bool = typer.Option(
        False,
        "--delete",
        help="Archive remote items absent from state/. NOT YET IMPLEMENTED — passing this flag is rejected.",
    ),
    only: Optional[str] = typer.Option(
        None,
        "--only",
        help="Comma-separated subset of: collections,snippets,cards,dashboards,pulses",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt. Implied when CI=true or stdout is not a TTY.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the optimistic-concurrency check (write even if remote items changed since plan).",
    ),
    allow_missing_recipients: bool = typer.Option(
        False,
        "--allow-missing-recipients",
        help="Silently drop pulse recipients whose email doesn't exist on the target. Default: error.",
    ),
    backup_dir: Optional[Path] = typer.Option(
        None,
        "--backup-dir",
        help="Run a fresh export to this path before applying. "
        "If apply trashes something, restore via `--state-dir <backup>`.",
    ),
) -> None:
    """Push the state tree to Metabase. Re-derives the diff at execution time."""
    if delete:
        # Reject before any HTTP call so a partial apply can't precede the NotImplementedError.
        raise typer.BadParameter(
            "--delete is not yet implemented. "
            "Run without --delete and archive orphans manually for now.",
            param_hint="--delete",
        )

    from metabase_sync.apply import run
    from metabase_sync.plan import render_report

    s = load_settings(state_dir=state_dir)
    only_set = _parse_only(only)

    with _client_from_settings(s) as client:
        # Plan first so the user sees what will happen.
        preview = run(client, s.state_dir, mode="plan", only=only_set)
        typer.echo(render_report(preview, s.state_dir))

        if preview.total_writes() == 0:
            raise typer.Exit(code=0)

        if backup_dir is not None:
            from metabase_sync.export import run_export

            typer.echo(f"\nbacking up current remote to {backup_dir}…")
            run_export(client, backup_dir)
            typer.echo(f"backup complete: {backup_dir}")

        if not yes and _is_interactive():
            confirmed = typer.confirm("\nApply these changes?", default=False)
            if not confirmed:
                typer.echo("aborted.")
                raise typer.Exit(code=1)

        executed = run(
            client,
            s.state_dir,
            mode="apply",
            only=only_set,
            force=force,
            allow_missing_recipients=allow_missing_recipients,
            concurrency_snapshot=preview.concurrency_snapshot,
        )

    # Write audit log so post-mortems aren't reliant on terminal scrollback.
    last_apply_path = s.state_dir / ".last-apply.json"
    last_apply_path.parent.mkdir(parents=True, exist_ok=True)
    last_apply_path.write_text(
        json.dumps(executed.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    typer.echo(f"\napplied. audit log: {last_apply_path}")


@app.command()
def index(
    state_dir: Optional[Path] = typer.Option(None, "--state-dir", "-s"),
) -> None:
    """Debug: print remote resource counts indexed by entity_id."""
    from metabase_sync.diff import build_remote_index

    s = load_settings(state_dir=state_dir)
    with _client_from_settings(s) as client:
        idx = build_remote_index(client)
    typer.echo(json.dumps(idx.summary(), indent=2))


@app.command()
def init(
    target: Path = typer.Argument(
        Path("."),
        help="Directory to scaffold. Defaults to the current directory.",
    ),
    metabase_url: Optional[str] = typer.Option(
        None, "--url", help="Pre-fill METABASE_URL in the .env."
    ),
) -> None:
    """Scaffold a new state directory: .env, .gitignore, README.

    Run this once at the top of a repo where you want to manage Metabase as code.
    """
    target.mkdir(parents=True, exist_ok=True)

    env_example = target / ".env.example"
    env = target / ".env"
    gitignore = target / ".gitignore"
    readme = target / "README.md"

    url_line = metabase_url or "https://metabase.example.com"
    env_template = f"METABASE_URL={url_line}\nMETABASE_API_KEY=\n"
    if not env_example.exists():
        env_example.write_text(env_template)
    if not env.exists():
        env.write_text(env_template)
    if not gitignore.exists():
        gitignore.write_text(".env\nstate/.plan.json\nstate/.last-apply.json\n")
    if not readme.exists():
        readme.write_text(
            "# Metabase state\n\n"
            "Managed by [metabase-sync](https://github.com/novucs/metabase-sync).\n\n"
            "## Quick start\n\n"
            "1. Mint an admin API key in your Metabase: Settings → Admin → "
            "Authentication → API keys.\n"
            "2. Paste it into `.env` as `METABASE_API_KEY=...`.\n"
            "3. `metabase-sync export`  # pulls the current state into `state/`.\n"
            "4. Commit `state/` to git.\n\n"
            "Day-to-day: edit files, `metabase-sync plan`, `metabase-sync apply`.\n"
        )

    typer.echo(f"scaffolded {target.resolve()}")
    typer.echo("  next steps:")
    typer.echo("  1. fill in METABASE_API_KEY in .env (mint from Metabase admin UI)")
    typer.echo("  2. metabase-sync export")


@app.command()
def diagnose(
    state_dir: Optional[Path] = typer.Option(None, "--state-dir", "-s"),
) -> None:
    """Print everything we'd ask for in a bug report. Paste into the issue."""
    import platform

    s = load_settings(state_dir=state_dir)
    typer.echo(f"metabase-sync version: {__version__}")
    typer.echo(f"python: {platform.python_version()} ({platform.system()})")
    typer.echo(f"METABASE_URL: {s.metabase_url}")
    typer.echo(f"METABASE_API_KEY: <redacted> (length {len(s.metabase_api_key)})")
    typer.echo(f"STATE_DIR: {s.state_dir}")
    typer.echo(f"HTTP_TIMEOUT_S: {s.http_timeout_s}")

    # State tree counts
    if s.state_dir.exists():
        counts = {
            "collections": len(list(s.state_dir.rglob("_collection.yaml"))),
            "snippets": len(list((s.state_dir / "snippets").glob("*.sql")))
            + len(list((s.state_dir / "collections").rglob("snippets/*.sql"))),
            "cards (sql)": len(
                list((s.state_dir / "collections").rglob("cards/*.sql"))
            ),
            "cards (yaml)": len(
                list((s.state_dir / "collections").rglob("cards/*.yaml"))
            ),
            "dashboards": len(list(s.state_dir.rglob("dashboard.yaml"))),
            "pulses": len(list((s.state_dir / "pulses").glob("*.yaml"))),
        }
        typer.echo("state tree:")
        for k, v in counts.items():
            typer.echo(f"  {k}: {v}")
    else:
        typer.echo(f"state tree: <{s.state_dir} does not exist>")

    # Live instance info
    try:
        with _client_from_settings(s) as client:
            props = client.get("/api/session/properties")
            tag = (props.get("version") or {}).get("tag") or "<unknown>"
            typer.echo(f"Metabase version: {tag}")
            health = client.get("/api/health")
            typer.echo(f"Metabase /api/health: {health}")
    except Exception as e:  # noqa: BLE001
        typer.echo(f"Could not reach instance: {type(e).__name__}: {e}")


def main() -> None:
    """Script entry point. Wraps `app()` so any `MetabaseSyncError` becomes a
    clean `exit 1` message instead of a Python traceback."""
    try:
        app()
    except MetabaseSyncError as exc:
        typer.secho(f"{exc}", err=True)
        raise typer.Exit(code=1) from None
