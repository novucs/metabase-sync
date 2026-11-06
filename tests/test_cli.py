import pytest

from metabase_sync import cli
from metabase_sync.store import tree, yamlio
from metabase_sync.sync import commands as workflow


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
    broken = ["card 'A' refers to 1 table", "card 'B' refers to 2 fields"]
    result = workflow.ExportResult(
        files=tree.Changes([], [], [], []),
        kept=[],
        conflicts=[],
        warnings=[],
        broken=broken + broken,
    )
    monkeypatch.setattr(workflow, "export", lambda *args, **kwargs: result)

    run(monkeypatch, "export", "--chdir", str(tmp_path), *["-v"] * verbose)

    printed = capsys.readouterr().err
    assert ("card 'A'" in printed) is verbose
    assert ("2 items refer to" in printed) is not verbose
    assert printed.count("warning:") == 2 - (not verbose)
