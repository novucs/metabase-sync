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
