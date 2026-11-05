import logging
import sys
from pathlib import Path

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.text import Text

from metabase_sync import __version__
from metabase_sync.errors import ConflictError, MetabaseSyncError, ValidationFailed
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.settings import load_settings
from metabase_sync.store.tree import CONFIG, WORKDIR, read_tree, require
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.operations import (
    ARCHIVE,
    CREATE,
    DELETE,
    IMPORT,
    LINKS,
    RESTORE,
    UPDATE,
    Drift,
    Operation,
    Plan,
)
from metabase_sync.sync.validate import validate

ISSUES = "https://github.com/novucs/metabase-sync/issues"
EXAMPLES = (
    "Examples:\n\n"
    "  metabase-sync init\n\n"
    "  metabase-sync export          pull Metabase into this directory\n\n"
    "  metabase-sync status          what changed here and in Metabase\n\n"
    "  metabase-sync plan            show the diff apply would make\n\n"
    "  metabase-sync apply           apply it, after asking\n\n"
    "Set METABASE_URL and METABASE_API_KEY first. Docs: "
    "https://github.com/novucs/metabase-sync"
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Metabase as code: keep collections, questions, dashboards, snippets, and "
    "subscriptions in git, in the metabase/representations format.",
    epilog=EXAMPLES,
)
out = Console(highlight=False)
err = Console(stderr=True, highlight=False)
options = {"verbose": False, "debug": False}

CHDIR = typer.Option(
    Path("."),
    "--chdir",
    help="Run in this directory instead of the current one.",
    envvar="METABASE_SYNC_CHDIR",
)
FORCE_HELP = "Overwrite changes made in Metabase since the last sync."
MARK = {
    CREATE: ("+", "green"),
    IMPORT: ("<", "blue"),
    RESTORE: ("+", "cyan"),
    UPDATE: ("~", "yellow"),
    ARCHIVE: ("-", "red"),
    DELETE: ("-", "bold red"),
}
PRECEDENCE = (CREATE, IMPORT, RESTORE, UPDATE, DELETE, ARCHIVE)


def _client(root: Path) -> MetabaseClient:
    require(root)
    return MetabaseClient(load_settings())


def _version(value: bool) -> None:
    if value:
        out.print(f"metabase-sync {__version__}")
        raise typer.Exit()


@app.callback()
def main_options(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show every file and HTTP request."
    ),
    debug: bool = typer.Option(False, "--debug", help="Show tracebacks on errors."),
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version."
    ),
) -> None:
    options.update(verbose=verbose, debug=debug)
    if verbose:
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
        logging.getLogger("urllib3").setLevel(logging.DEBUG)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("warning: %(message)s"))
    logger = logging.getLogger("metabase_sync")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)


class Item(BaseModel):
    entity_id: str
    kind: str
    name: str
    path: str | None
    actions: list[str] = []
    notes: list[str] = []

    @property
    def action(self) -> str:
        return next(a for a in PRECEDENCE if a in self.actions)


def _items(plan: Plan) -> list[Item]:
    found: dict[str, Item] = {}
    for op in plan.operations:
        item = found.setdefault(
            op.entity_id,
            Item(entity_id=op.entity_id, kind=op.kind, name=op.name, path=op.path),
        )
        item.actions.append(op.action)
        if op.part == LINKS:
            item.notes.append("then links to items this apply creates")
        elif op.reason:
            item.notes.append(op.reason)
    overwritten = plan.drifted if plan.force else set()
    for item in found.values():
        if item.entity_id in overwritten:
            item.notes.append("overwrites a change made in Metabase")
    return list(found.values())


def _title(
    mark: str, style: str, words: str, kind: str, name: str, path: str | None
) -> Text:
    line = Text("  ")
    line.append(f"{mark} {words}", style=style)
    line.append(f" {kind} {name!r}")
    if path:
        line.append(f"  {path}", style="dim")
    return line


def _entry_line(mark: str, style: str, entry: Drift) -> Text:
    line = Text("  ")
    line.append(mark, style=style)
    line.append(f" {entry.kind} {entry.name!r}")
    if entry.path:
        line.append(f"  {entry.path}", style="dim")
    line.append(f"  {entry.change}", style=style)
    return line


def _print_diff(diff: str) -> None:
    for raw in diff.splitlines():
        style = (
            "dim"
            if raw.startswith(("---", "+++", "@@"))
            else "green"
            if raw.startswith("+")
            else "red"
            if raw.startswith("-")
            else ""
        )
        out.print(Text(f"      {raw}", style=style))


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _print_plan(plan: Plan) -> None:
    items = _items(plan)
    for item in items:
        mark, style = MARK[item.action]
        out.print(_title(mark, style, item.action, item.kind, item.name, item.path))
        for note in item.notes:
            out.print(Text(f"      {note}", style="dim"))
        if item.action in (IMPORT, UPDATE, RESTORE) and item.entity_id in plan.diffs:
            _print_diff(plan.diffs[item.entity_id])
    if items:
        tally: dict[str, int] = {}
        for item in items:
            tally[item.action] = tally.get(item.action, 0) + 1
        out.print(
            "\nPlan: "
            + ", ".join(f"{n} to {a}" for a in PRECEDENCE if (n := tally.get(a)))
            + "."
        )
    elif plan.drift and not plan.force:
        out.print("Nothing to apply.")
    elif not plan.conflicts:
        out.print("[green]No changes.[/green] Metabase matches the files.")
    if plan.drift and not plan.force:
        out.print(
            f"\n{_count(len(plan.drift), 'item')} changed in Metabase since the last sync. "
            "Apply leaves them alone; `metabase-sync export` takes them, `--force` overwrites them:"
        )
        for entry in plan.drift:
            out.print(_entry_line("~", "cyan", entry))
            if options["verbose"] and entry.entity_id in plan.diffs:
                _print_diff(plan.diffs[entry.entity_id])
    if plan.conflicts and not plan.force:
        _print_conflicts(plan.conflicts, plan.diffs)
    if plan.cascaded:
        out.print(
            f"\n{_count(len(plan.cascaded), 'item')} go with their archived collection or dashboard:"
        )
        for ref in plan.cascaded:
            out.print(Text(f"  - {ref.kind} {ref.name!r}", style="dim"))
    if plan.unmanaged:
        out.print(
            f"[dim]{_count(len(plan.unmanaged), 'item')} exist only in Metabase and are left "
            "alone; `metabase-sync export` brings them in.[/dim]"
        )


def _print_conflicts(conflicts: list[Drift], diffs: dict[str, str]) -> None:
    out.print(
        f"\n{_count(len(conflicts), 'conflict')} with Metabase. "
        "Apply stops until you resolve them:"
    )
    for entry in conflicts:
        out.print(_entry_line("!", "bold red", entry))
        if entry.entity_id in diffs:
            _print_diff(diffs[entry.entity_id])


def _print_warnings(warnings: list[str]) -> None:
    for warning in dict.fromkeys(warnings):
        err.print(f"[yellow]warning:[/yellow] {warning}")


def _print_notes(notes: list[str]) -> None:
    for note in notes:
        err.print(f"[cyan]note:[/cyan] {note}")


@app.command(help=f"Create {CONFIG} and a .gitignore.")
def init(root: Path = CHDIR) -> None:
    root.mkdir(parents=True, exist_ok=True)
    config = root / CONFIG
    if config.exists():
        err.print(f"{config} already exists.")
        return
    config.write_text("removal: archive\n", encoding="utf-8")
    gitignore = root / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    if f"{WORKDIR}/" not in lines:
        lines.append(f"{WORKDIR}/")
    gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
    err.print(
        f"Wrote {config}.\n\n"
        "Next, set METABASE_URL and METABASE_API_KEY, then run `metabase-sync export`.\n"
        f"To manage only some collections, list their entity ids under `collections:` in {config}."
    )


@app.command(help="Pull Metabase into the files, keeping edits you haven't applied.")
def export(
    root: Path = CHDIR,
    check: bool = typer.Option(
        False, "--check", help="Write nothing; exit 2 if the files are out of date."
    ),
    force: bool = typer.Option(
        False, "--force", help="Take Metabase's version even over unapplied edits."
    ),
) -> None:
    with _client(root) as client, err.status("Exporting from Metabase"):
        result = workflow.export(client, root, check=check, force=force)
    _print_warnings(result.warnings)
    if result.conflicts:
        _print_conflicts(result.conflicts, {})
        err.print(
            "\n[red]error:[/red] nothing written.\n"
            "[dim]hint: apply or commit your edits, then run `metabase-sync export --force` "
            "to take Metabase's versions[/dim]"
        )
        raise typer.Exit(1)
    if result.kept:
        out.print(
            f"Kept {_count(len(result.kept), 'unapplied edit')} (`metabase-sync apply` pushes them):"
        )
        for path in result.kept:
            out.print(f"  {path}")
    if not (result.added or result.changed or result.removed):
        out.print("[green]Files are up to date.[/green]")
        return
    if options["verbose"]:
        for label, paths, style in (
            ("added", result.added, "green"),
            ("changed", result.changed, "yellow"),
            ("removed", result.removed, "red"),
        ):
            for path in paths:
                out.print(f"[{style}]{label:8}[/{style}] {path}")
    counts = ", ".join(
        f"{len(paths)} {label}"
        for label, paths in (
            ("added", result.added),
            ("changed", result.changed),
            ("removed", result.removed),
        )
        if paths
    )
    verb = "Files are out of date" if check else "Updated the files"
    hint = "" if options["verbose"] else " Pass --verbose to list the files."
    out.print(f"{verb}: {counts} files.{hint}")
    if check:
        raise typer.Exit(2)


@app.command(name="validate", help="Check the files offline.")
def validate_command(root: Path = CHDIR) -> None:
    require(root)
    report = validate(read_tree(root))
    _print_warnings(report.warnings)
    if report.errors:
        raise ValidationFailed(report.errors)
    out.print("[green]Files are valid.[/green]")


@app.command(help="Give files entity ids and rewrite them in canonical form and place.")
def fmt(root: Path = CHDIR) -> None:
    assigned, moved = workflow.fmt(root)
    for path in assigned:
        out.print(f"[green]assigned ids[/green] {path}")
    for path in moved:
        out.print(f"[yellow]wrote[/yellow]       {path}")
    if not assigned and not moved:
        out.print("[green]Already formatted.[/green]")


@app.command(help="Show what apply would change, as a diff against Metabase.")
def plan(
    root: Path = CHDIR,
    out_file: Path | None = typer.Option(
        None, "--out", help="Save the plan for `apply PLANFILE`."
    ),
    detailed_exitcode: bool = typer.Option(
        False, "--detailed-exitcode", help="Exit 2 when there are changes or conflicts."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the plan as JSON."),
    force: bool = typer.Option(False, "--force", help=FORCE_HELP),
) -> None:
    with _client(root) as client, err.status("Planning"):
        prepared, result = workflow.plan(client, root, force)
    _print_notes(prepared.notes)
    _print_warnings(prepared.report.warnings + prepared.remote.index.warnings)
    if as_json:
        out.print_json(result.model_dump_json())
    else:
        _print_plan(result)
    if out_file is not None:
        out_file.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        err.print(f"Saved to {out_file}; run `metabase-sync apply {out_file}`.")
    if detailed_exitcode and (result.operations or result.conflicts):
        raise typer.Exit(2)


@app.command(help="Apply the plan to Metabase, after asking.")
def apply(
    plan_file: Path | None = typer.Argument(
        None, help="A plan saved with `plan --out`; applied without asking."
    ),
    root: Path = CHDIR,
    auto_approve: bool = typer.Option(
        False, "--auto-approve", "-y", help="Apply without asking."
    ),
    force: bool = typer.Option(False, "--force", help=FORCE_HELP),
) -> None:
    def approve(prepared: workflow.Prepared, result: Plan) -> bool:
        _print_plan(result)
        if auto_approve:
            return True
        if not sys.stdin.isatty():
            err.print(
                "\nNot applying: no terminal to confirm in. "
                "Pass --auto-approve, or apply a plan saved with `plan --out`."
            )
            return False
        return typer.confirm("\nApply these changes?", default=False)

    def done(op: Operation, detail: str) -> None:
        mark, style = MARK[op.action]
        line = _title(mark, style, op.action, op.kind, op.name, None)
        if detail:
            line.append(f"  {detail}", style="dim")
        out.print(line)

    with _client(root) as client:
        prepared, result, applied = workflow.apply(
            client, root, plan_file, approve, done, force=force
        )
    _print_notes(prepared.notes)
    if result.empty:
        if plan_file is None:
            _print_plan(result)
        else:
            out.print("[green]No changes.[/green]")
    elif applied:
        out.print(f"\n[green]Applied {_count(len(_items(result)), 'change')}.[/green]")
    else:
        raise typer.Exit(1)


@app.command(
    help="Show interrupted runs, local and Metabase changes, conflicts, and other problems."
)
def status(root: Path = CHDIR) -> None:
    with _client(root) as client, err.status("Checking"):
        result = workflow.status(client, root)
    if result.interrupted:
        out.print(
            "[red]An apply was interrupted.[/red] Run `metabase-sync apply` to finish it."
        )
    for error in result.report.errors:
        out.print(f"[red]error:[/red] {error}")
    for warning in [*result.report.warnings, *result.duplicates]:
        out.print(f"[yellow]warning:[/yellow] {warning}")
    current = result.plan
    if current is None:
        raise typer.Exit(1)
    items = _items(current)
    if items:
        out.print(
            f"{_count(len(items), 'change')} to apply (`metabase-sync plan` shows the diff):"
        )
        for item in items:
            mark, style = MARK[item.action]
            out.print(_title(mark, style, item.action, item.kind, item.name, item.path))
    if current.drift:
        out.print(
            f"{_count(len(current.drift), 'item')} changed in Metabase (`metabase-sync export` takes them):"
        )
        for entry in current.drift:
            out.print(_entry_line("~", "cyan", entry))
    if current.conflicts:
        _print_conflicts(current.conflicts, {})
    if current.unmanaged:
        out.print(
            f"{_count(len(current.unmanaged), 'item')} only in Metabase (`metabase-sync export` brings them in)."
        )
    if not (items or current.drift or current.conflicts or result.report.errors):
        out.print("[green]In sync with Metabase.[/green]")
    if result.report.errors or current.conflicts:
        raise typer.Exit(1)


def main() -> None:
    try:
        app()
    except ConflictError as e:
        if options["debug"]:
            raise
        _print_conflicts(e.conflicts, {})
        err.print(f"\n[red]error:[/red] {e}\n[dim]hint: {e.hint}[/dim]")
        sys.exit(1)
    except ValidationFailed as e:
        if options["debug"]:
            raise
        for problem in e.problems:
            err.print(f"[red]error:[/red] {problem}")
        sys.exit(1)
    except MetabaseSyncError as e:
        if options["debug"]:
            raise
        err.print(f"[red]error:[/red] {e}")
        if e.hint:
            err.print(f"[dim]hint: {e.hint}[/dim]")
        sys.exit(1)
    except Exception as e:
        if options["debug"]:
            raise
        err.print(f"[red]error:[/red] unexpected {type(e).__name__}: {e}")
        err.print(
            f"[dim]hint: run again with --debug for the traceback, and report it at {ISSUES}[/dim]"
        )
        sys.exit(1)
