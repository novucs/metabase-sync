from types import SimpleNamespace

import pytest

from metabase_sync import cli
from metabase_sync.store import tree, yamlio
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.operations import Drift, Kept, Plan


def run(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["metabase-sync", *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    return exited.value.code if isinstance(exited.value.code, int) else 0


def test_no_command_prints_help_and_succeeds(monkeypatch, capsys):
    code = run(monkeypatch)

    printed = capsys.readouterr()
    assert code == 0
    assert "Usage" in printed.out and "error:" not in printed.err


def test_usage_errors_exit_1_not_the_changes_found_code(tmp_path, monkeypatch):
    yamlio.write(tmp_path / tree.CONFIG, {})

    code = run(monkeypatch, "apply", str(tmp_path / "missing.json"))

    assert code == 1


def test_ctrl_c_exits_130_and_says_so(tmp_path, monkeypatch, capsys):
    yamlio.write(tmp_path / tree.CONFIG, {})
    monkeypatch.setenv("METABASE_URL", "http://mb.test")
    monkeypatch.setenv("METABASE_API_KEY", "k")

    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "export", interrupted)

    code = run(monkeypatch, "export", "--chdir", str(tmp_path))

    assert code == 130
    assert "Interrupted." in capsys.readouterr().err


@pytest.mark.parametrize("verbose", [False, True])
def test_items_with_missing_references_are_summarised_unless_verbose(
    tmp_path, monkeypatch, capsys, verbose
):
    yamlio.write(tmp_path / tree.CONFIG, {})
    monkeypatch.setenv("METABASE_URL", "http://mb.test")
    monkeypatch.setenv("METABASE_API_KEY", "k")
    broken = ["card 'A' uses table 1", "card 'B' uses fields 2, 3"]
    missing = ["card 'C' refers to database 9", "card 'D' refers to card 8"]
    result = workflow.ExportResult(
        files=tree.Changes([], [], [], []),
        kept=[],
        conflicts=[],
        warnings=[],
        broken=broken + broken,
        missing=missing,
    )
    monkeypatch.setattr(workflow, "export", lambda *args, **kwargs: result)

    run(monkeypatch, "export", "--chdir", str(tmp_path), *["-v"] * verbose)

    printed = capsys.readouterr().err
    assert ("card 'A'" in printed) is verbose
    assert ("card 'C'" in printed) is verbose
    assert "2 items use tables or fields that no longer exist" in printed
    assert "2 items refer to things Metabase doesn't list" in printed
    assert ("--verbose lists them" in printed) is not verbose
    assert printed.count("warning:") == (6 if verbose else 2)


def test_items_kept_despite_unmanaged_removal_are_listed_with_reasons(capsys):
    plan = Plan(
        metabase="v0.65.3",
        kept=[
            Kept(
                kind="collection",
                entity_id="N" * 21,
                name="Notes",
                reason="holds document 'Plan'",
            )
        ],
    )

    cli._print_plan(plan)

    printed = capsys.readouterr().out
    assert "Kept, though only in Metabase:" in printed
    assert "collection 'Notes'  holds document 'Plan'" in printed
    assert "export` brings" not in printed


def test_plan_exits_1_on_a_blocked_removal_even_with_force(
    tmp_path, monkeypatch, capsys
):
    yamlio.write(tmp_path / tree.CONFIG, {})
    monkeypatch.setenv("METABASE_URL", "http://mb.test")
    monkeypatch.setenv("METABASE_API_KEY", "k")
    blocked = Drift(
        kind="collection",
        entity_id="F" * 21,
        name="Finance",
        change="removed here, but holds document 'Q3 notes'",
    )
    index = SimpleNamespace(warnings=[], broken=[], missing=[])
    prepared = SimpleNamespace(
        notes=[],
        report=SimpleNamespace(warnings=[]),
        remote=SimpleNamespace(index=index),
    )
    plan = Plan(metabase="v0.65.3", force=True, blocked=[blocked])
    monkeypatch.setattr(workflow, "plan", lambda *args, **kwargs: (prepared, plan))

    code = run(monkeypatch, "plan", "--chdir", str(tmp_path), "--force")

    printed = capsys.readouterr()
    assert code == 1
    assert "1 removal can't be applied, even with --force:" in printed.out
    assert "collection 'Finance'" in printed.out
