import logging
import sys
import threading
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.highlighter import NullHighlighter
from rich.logging import RichHandler
from rich.markup import escape
from rich.text import Text

from metabase_sync import __version__
from metabase_sync.errors import (
    BlockedError,
    ConfigError,
    ConflictError,
    MetabaseSyncError,
    ValidationFailed,
    count,
)
from metabase_sync.metabase.client import MetabaseClient
from metabase_sync.settings import load_settings
from metabase_sync.store.journal import Journal
from metabase_sync.store.tree import CONFIG, WORKDIR, Changes, read_tree, require
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.commands import load_config
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
    pretty_exceptions_enable=False,
    help="Metabase as code: keep collections, questions, dashboards, snippets, and "
    "subscriptions in git, in the metabase/representations format.",
    epilog=EXAMPLES,
)
out = Console(highlight=False, soft_wrap=True)
err = Console(stderr=True, highlight=False, soft_wrap=True)


@dataclass
class Options:
    verbose: bool = False
    debug: bool = False
    root: Path = Path(".")


options = Options()

Root = Annotated[
    Path | None,
    typer.Option(
        "--chdir",
        help="Run in this directory instead of the current one.",
        envvar="METABASE_SYNC_CHDIR",
        show_default=False,
    ),
]
Verbose = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Show every item and HTTP request."),
]
Debug = Annotated[bool, typer.Option("--debug", help="Show tracebacks on errors.")]
AutoApprove = Annotated[
    bool, typer.Option("--auto-approve", "-y", help="Don't ask first.")
]
Force = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Go ahead even if there are conflicts.",
    ),
]
MARK = {
    CREATE: ("+", "green"),
    IMPORT: ("<", "blue"),
    RESTORE: ("+", "cyan"),
    UPDATE: ("~", "yellow"),
    ARCHIVE: ("-", "red"),
    DELETE: ("-", "bold red"),
}
PRECEDENCE = (CREATE, RESTORE, IMPORT, UPDATE, DELETE, ARCHIVE)


def _start(root: Path | None, verbose: bool, debug: bool) -> Path:
    options.verbose |= verbose
    options.debug |= debug
    handler = RichHandler(
        console=err,
        show_time=False,
        show_level=False,
        show_path=False,
        highlighter=NullHighlighter(),
    )
    if options.verbose:
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        handler.setFormatter(logging.Formatter("warning: %(message)s"))
        logger = logging.getLogger("metabase_sync")
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
    options.root = root or options.root
    return options.root


def _client(root: Path) -> MetabaseClient:
    require(root)
    return MetabaseClient(load_settings(root))


@contextmanager
def _progress(
    client: MetabaseClient, text: str = "Reading the files"
) -> Iterator[None]:
    def fit(text: str) -> Text:
        line = Text(text)
        line.truncate(max(err.width - 2, 1), overflow="ellipsis")
        return line

    previous = client.report
    with err.status(fit(text)) as status:
        client.report = lambda text: status.update(fit(text))
        try:
            yield
        finally:
            client.report = previous


def _version(value: bool) -> None:
    if value:
        out.print(f"metabase-sync {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_options(
    ctx: typer.Context,
    root: Root = None,
    verbose: Verbose = False,
    debug: Debug = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show the version."
        ),
    ] = False,
) -> None:
    options.verbose, options.debug = verbose, debug
    options.root = root or Path(".")
    if ctx.invoked_subcommand is None:
        out.print(ctx.get_help())


def _agree(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


@dataclass
class Item:
    entity_id: str
    kind: str
    name: str
    path: str | None
    actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

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
    for entry in plan.drift:
        if entry.entity_id in found:
            found[entry.entity_id].notes.append(f"{entry.change} since the last sync")
    return list(found.values())


def _print_undone(plan: Plan) -> None:
    listed = {op.entity_id for op in plan.operations}
    n = len({d.entity_id for d in plan.drift} & listed)
    if n:
        out.print(
            f"{n} of these {_agree(n, 'undoes a change', 'undo changes')} made in "
            f"Metabase. Run `metabase-sync export` first to keep "
            f"{_agree(n, 'it', 'them')}."
        )


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


def _print_unmanaged(plan: Plan) -> None:
    if plan.kept:
        out.print("\nKept, though only in Metabase:")
        for kept in plan.kept:
            line = Text(f"  {kept.kind} {kept.name!r}")
            line.append(f"  {kept.reason}", style="dim")
            out.print(line)
    if not plan.unmanaged:
        return
    n = len(plan.unmanaged)
    out.print(
        f"[dim]{count(n, 'item')} {_agree(n, 'exists', 'exist')} only in Metabase "
        f"and {_agree(n, 'is', 'are')} left alone; `metabase-sync export` brings "
        f"{_agree(n, 'it', 'them')} in.[/dim]"
    )
    if options.verbose:
        for ref in plan.unmanaged:
            out.print(Text(f"  {ref.kind} {ref.name!r}", style="dim"))


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
        tally = Counter(item.action for item in items)
        out.print(
            "\nPlan: "
            + ", ".join(f"{tally[a]} to {a}" for a in PRECEDENCE if tally[a])
            + "."
        )
        _print_undone(plan)
        if tally[DELETE]:
            out.print(
                f"[bold red]{count(tally[DELETE], 'item')} will be permanently "
                "deleted.[/bold red]"
            )
    elif not plan.conflicts:
        out.print("[green]No changes.[/green] Metabase matches the files.")
    if plan.conflicts:
        _print_conflicts(plan.conflicts, plan.force)
    if plan.blocked:
        _print_blocked(plan.blocked)
    if plan.cascaded:
        n = len(plan.cascaded)
        out.print(
            f"\n{count(n, 'item')} {_agree(n, 'goes', 'go')} with "
            "the archived collection or dashboard that holds it:"
        )
        for ref in plan.cascaded:
            out.print(Text(f"  - {ref.kind} {ref.name!r}", style="dim"))
    if plan.unmanaged or plan.kept:
        _print_unmanaged(plan)


def _print_blocked(blocked: list[Drift]) -> None:
    n = len(blocked)
    out.print(f"\n{count(n, 'removal')} can't be applied, even with --force:")
    for entry in blocked:
        out.print(_entry_line("!", "bold red", entry))


def _print_conflicts(conflicts: list[Drift], forced: bool = False) -> None:
    n = len(conflicts)
    out.print(
        f"\n{count(n, 'conflict')}, ignored because of --force:"
        if forced
        else f"\n{count(n, 'conflict')} with Metabase. Fix "
        f"{_agree(n, 'it', 'them')} before applying:"
    )
    for entry in conflicts:
        out.print(_entry_line("!", "bold red", entry))


def _print_error(error: MetabaseSyncError) -> None:
    err.print(f"[red]error:[/red] {escape(str(error))}")
    _print_hint(error)


def _print_hint(error: MetabaseSyncError) -> None:
    if error.hint:
        err.print(f"[dim]hint: {escape(error.hint)}[/dim]")


BROKEN = (
    "{} items use tables or fields that no longer exist in the database; "
    "check them in Metabase."
)
MISSING = (
    "{} items refer to things Metabase doesn't list; "
    "metabase-sync keeps their numeric ids."
)


def _print_warnings(
    warnings: list[str],
    *,
    broken: Sequence[str] = (),
    missing: Sequence[str] = (),
    console: Console = err,
) -> None:
    lines = list(warnings)
    for found, summary in ((broken, BROKEN), (missing, MISSING)):
        found = list(dict.fromkeys(found))
        if len(found) == 1:
            lines += found
        elif found:
            head = summary.format(len(found))
            lines += (
                [head, *found] if options.verbose else [f"{head} --verbose lists them."]
            )
    for warning in dict.fromkeys(lines):
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")


def _print_notes(notes: list[str]) -> None:
    for note in notes:
        err.print(f"[cyan]note:[/cyan] {escape(note)}")


def _print_files(changes: Changes, notes: dict[str, str]) -> None:
    lines = [
        *(("+", "green", path, notes.get(path)) for path in changes.added),
        *(("~", "yellow", path, notes.get(path)) for path in changes.changed),
        *(
            (">", "cyan", f"{old} -> {new}", notes.get(new) or notes.get(old))
            for old, new in changes.moved
        ),
        *(("-", "red", path, notes.get(path)) for path in changes.removed),
    ]
    for mark, style, text, note in lines:
        out.print(Text(f"  {mark} {text}", style=style))
        if note:
            out.print(Text(f"      {note}", style="dim"))


@app.command(help=f"Create {CONFIG} and a .gitignore.")
def init(
    root: Root = None,
    collection: Annotated[
        list[str] | None,
        typer.Option(
            "--collection",
            help="Manage only this collection, by name or entity id. Repeatable.",
        ),
    ] = None,
    verbose: Verbose = False,
    debug: Debug = False,
) -> None:
    root = _start(root, verbose, debug)
    config = root / CONFIG
    found = []
    if collection:
        with MetabaseClient(load_settings(root)) as client:
            found = workflow.find_collections(client, collection)
    listed = "".join(
        f"  - {eid}  # {' '.join(where.split())}\n" for eid, where in found
    )
    if config.exists():
        if found:
            err.print(
                f"{config} already exists. Add these under `collections:`:",
                markup=False,
            )
            err.print(listed.rstrip("\n"), markup=False)
        else:
            err.print(f"{config} already exists.", markup=False)
        return
    root.mkdir(parents=True, exist_ok=True)
    scope = f"collections:\n{listed}" if found else ""
    config.write_text(f"{scope}removal: archive\n", encoding="utf-8")
    gitignore = root / ".gitignore"
    lines = gitignore.read_text().splitlines() if gitignore.exists() else []
    lines += [line for line in (f"{WORKDIR}/", ".env") if line not in lines]
    gitignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if found:
        err.print(f"Wrote {config}.\n\nNext, run `metabase-sync export`.", markup=False)
        return
    err.print(
        f"Wrote {config}.\n\n"
        "Next, set METABASE_URL and METABASE_API_KEY if you haven't, then run "
        "`metabase-sync export`.\nTo manage only some collections, run "
        "`metabase-sync init --collection NAME` to look up their entity ids.",
        markup=False,
    )


@app.command(help="Make the files match Metabase, after asking.")
def export(
    root: Root = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Write nothing; exit 2 if the files are out of date."
        ),
    ] = False,
    auto_approve: AutoApprove = False,
    verbose: Verbose = False,
    debug: Debug = False,
) -> None:
    root = _start(root, verbose, debug)
    with _client(root) as client, _progress(client):
        result = workflow.pull(client, root)
    _print_warnings(result.warnings, broken=result.broken, missing=result.missing)
    _print_export(result)
    if check:
        raise typer.Exit(0 if result.files.empty else 2)
    if result.files.empty:
        workflow.write(root, result)
        return
    _confirm(
        auto_approve,
        "Write these files?",
        "Not exporting: no terminal to confirm in. Pass --auto-approve.",
    )
    workflow.write(root, result)
    out.print("\n[green]Files written.[/green]")


def _print_export(result: workflow.ExportResult) -> None:
    files = result.files
    _print_files(files, result.overwrites)
    if files.empty:
        out.print("[green]Files are up to date.[/green]")
    else:
        counts = [
            (files.added, "add"),
            (files.changed, "change"),
            (files.moved, "move"),
            (files.removed, "remove"),
        ]
        tally = ", ".join(f"{len(found)} to {verb}" for found, verb in counts if found)
        out.print(f"\nExport: {tally}.")
    n = len(result.overwrites)
    if n:
        out.print(
            f"{n} of these {_agree(n, 'undoes a change', 'undo changes')} you "
            f"haven't applied. Run `metabase-sync apply` first to keep "
            f"{_agree(n, 'it', 'them')}."
        )
    n = len(result.new)
    if n:
        out.print(
            f"[dim]{count(n, 'file')} {_agree(n, "isn't", "aren't")} matched to an "
            f"item in Metabase and {_agree(n, 'was', 'were')} left as "
            f"{_agree(n, 'it is', 'they are')}.[/dim]"
        )


def _confirm(auto_approve: bool, question: str, refusal: str) -> None:
    if auto_approve:
        return
    if not sys.stdin.isatty():
        err.print(f"\n{refusal}")
        raise typer.Exit(1)
    if not typer.confirm(f"\n{question}", default=False):
        raise typer.Exit(1)


@app.command(name="validate", help="Check the files offline.")
def validate_command(
    root: Root = None, verbose: Verbose = False, debug: Debug = False
) -> None:
    root = _start(root, verbose, debug)
    require(root)
    report = validate(read_tree(root), load_config(root).collections)
    _print_warnings(report.warnings)
    if report.errors:
        raise ValidationFailed(report.errors)
    out.print("[green]Files are valid.[/green]")


@app.command(help="Give files entity ids and rewrite them in canonical form and place.")
def fmt(root: Root = None, verbose: Verbose = False, debug: Debug = False) -> None:
    root = _start(root, verbose, debug)
    assigned, changes = workflow.fmt(root)
    _print_files(changes, dict.fromkeys(assigned, "gets entity ids"))
    if changes.empty:
        out.print("[green]Already formatted.[/green]")


@app.command(help="Show what apply would change, as a diff against Metabase.")
def plan(
    root: Root = None,
    out_file: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Save the plan for `apply PLANFILE`.", dir_okay=False
        ),
    ] = None,
    detailed_exitcode: Annotated[
        bool,
        typer.Option(
            "--detailed-exitcode", help="Exit 2 when there are changes to apply."
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the plan as JSON.")
    ] = False,
    force: Force = False,
    verbose: Verbose = False,
    debug: Debug = False,
) -> None:
    root = _start(root, verbose, debug)
    with _client(root) as client, _progress(client):
        prepared, result = workflow.plan(client, root, force)
    _print_notes(prepared.notes)
    _print_warnings(
        prepared.report.warnings + prepared.remote.index.warnings,
        broken=prepared.remote.index.broken,
        missing=prepared.remote.index.missing,
    )
    if as_json:
        out.print_json(result.model_dump_json())
    else:
        _print_plan(result)
    if out_file is not None:
        try:
            out_file.write_text(
                result.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
        except OSError as e:
            raise ConfigError(
                f"Can't write the plan to {out_file}: {e.strerror}"
            ) from e
        err.print(
            f"Saved to {out_file}; run `metabase-sync apply {out_file}`.", markup=False
        )
    if result.blocked:
        _print_error(BlockedError(result.blocked))
        raise typer.Exit(1)
    if result.conflicts and not force:
        _print_error(ConflictError(result.conflicts))
        raise typer.Exit(1)
    if detailed_exitcode and result.operations:
        raise typer.Exit(2)


@app.command(help="Apply the plan to Metabase, after asking.")
def apply(
    plan_file: Annotated[
        Path | None,
        typer.Argument(
            help="A plan saved with `plan --out`; applied without asking.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    root: Root = None,
    auto_approve: AutoApprove = False,
    force: Force = False,
    verbose: Verbose = False,
    debug: Debug = False,
) -> None:
    root = _start(root, verbose, debug)
    with _client(root) as client:
        with _progress(client):
            prepared, result = workflow.plan_apply(client, root, plan_file, force)
        _print_notes(prepared.notes)
        _print_warnings(
            prepared.report.warnings + prepared.remote.index.warnings,
            broken=prepared.remote.index.broken,
            missing=prepared.remote.index.missing,
        )
        if plan_file is None or result.empty:
            _print_plan(result)
        if plan_file is None and not result.empty:
            _confirm(
                auto_approve,
                "Apply these changes?",
                "Not applying: no terminal to confirm in. Pass --auto-approve, "
                "or apply a plan saved with `plan --out`.",
            )
        steps = Counter(op.entity_id for op in result.operations)
        total = len(steps)
        lock = threading.Lock()
        finished = 0
        screen = err if out.file.isatty() and err.file.isatty() else out

        def done(op: Operation, detail: str) -> None:
            nonlocal finished
            mark, style = MARK[op.action]
            line = _title(mark, style, op.action, op.kind, op.name, None)
            if detail:
                line.append(f"  {detail}", style="dim")
            with lock:
                steps[op.entity_id] -= 1
                if not steps[op.entity_id]:
                    finished += 1
                    client.report(f"Applying {finished}/{total}")
            screen.print(line)

        with _progress(client, f"Applying 0/{total}") if total else nullcontext():
            workflow.apply(client, root, prepared, result, done)
    if not result.empty:
        out.print(f"\n[green]Applied {count(len(_items(result)), 'change')}.[/green]")


@app.command(
    help="Show interrupted runs, local and Metabase changes, conflicts, and other problems."
)
def status(root: Root = None, verbose: Verbose = False, debug: Debug = False) -> None:
    root = _start(root, verbose, debug)
    with _client(root) as client, _progress(client):
        result = workflow.status(client, root)
    if result.interrupted:
        out.print(
            "[red]An apply was interrupted.[/red] Run `metabase-sync apply` to finish it."
        )
    for error in result.report.errors:
        out.print(f"[red]error:[/red] {escape(error)}")
    _print_warnings(
        result.report.warnings + result.warnings,
        broken=result.broken,
        missing=result.missing,
        console=out,
    )
    current = result.plan
    if current is None:
        raise typer.Exit(1)
    items = _items(current)
    if items:
        out.print(
            f"{count(len(items), 'change')} to apply (`metabase-sync plan` shows the diff):"
        )
        for item in items:
            mark, style = MARK[item.action]
            out.print(_title(mark, style, item.action, item.kind, item.name, item.path))
            for note in item.notes:
                out.print(Text(f"      {note}", style="dim"))
        _print_undone(current)
    if current.conflicts:
        _print_conflicts(current.conflicts)
    if current.blocked:
        _print_blocked(current.blocked)
    problems = current.conflicts or current.blocked or result.report.errors
    if not (items or problems):
        out.print("[green]Files match Metabase.[/green]")
    if current.unmanaged or current.kept:
        _print_unmanaged(current)
    if problems:
        raise typer.Exit(1)


def main() -> None:
    try:
        code = app(standalone_mode=False)
    except typer.Abort:
        code = 130
    except Exception as error:
        if options.debug:
            raise
        match error:
            case typer.TyperException():
                err.print(f"[red]error:[/red] {escape(error.format_message())}")
                err.print("[dim]hint: run `metabase-sync --help` for usage[/dim]")
            case BlockedError():
                _print_blocked(error.blocked)
                err.print()
                _print_error(error)
            case ConflictError():
                _print_conflicts(error.conflicts)
                err.print()
                _print_error(error)
            case ValidationFailed():
                for problem in error.problems:
                    err.print(f"[red]error:[/red] {escape(problem)}")
                _print_hint(error)
            case MetabaseSyncError():
                _print_error(error)
            case _:
                err.print(
                    f"[red]error:[/red] unexpected {type(error).__name__}: "
                    + escape(str(error))
                )
                err.print(
                    f"[dim]hint: run again with --debug for the traceback, and report it at {ISSUES}[/dim]"
                )
        sys.exit(1)
    if code == 130:
        unfinished = bool(Journal(options.root).entries())
        err.print(
            "\nInterrupted."
            + (" Run `metabase-sync apply` to finish." if unfinished else "")
        )
    sys.exit(code)
