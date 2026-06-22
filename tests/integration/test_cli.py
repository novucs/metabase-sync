"""The typer CLI is the real user surface and is otherwise untested. Drive it
through CliRunner with env-injected credentials: exit codes, artifact files,
--backup-dir, init, diagnose."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from metabase_sync.cli import app

from ._builders import unique_name, write_collection

pytestmark = pytest.mark.integration

runner = CliRunner()


def _env(url: str, key: str) -> dict[str, str]:
    # CI=true makes apply non-interactive (no confirmation prompt).
    return {"METABASE_URL": url, "METABASE_API_KEY": key, "CI": "true"}


def test_plan_exit_2_when_pending_and_writes_plan_json(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    write_collection(state, "cli", unique_name("cli-pending"))
    result = runner.invoke(
        app,
        ["plan", "--state-dir", str(state)],
        env=_env(metabase_url, metabase_api_key),
    )
    assert result.exit_code == 2, result.output
    assert (state / ".plan.json").exists()


def test_plan_exit_0_when_clean(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    env = _env(metabase_url, metabase_api_key)
    assert (
        runner.invoke(app, ["export", "--state-dir", str(state)], env=env).exit_code
        == 0
    )
    result = runner.invoke(app, ["plan", "--state-dir", str(state)], env=env)
    assert result.exit_code == 0, result.output


def test_apply_writes_last_apply_json(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    write_collection(state, "cli", unique_name("cli-apply"))
    result = runner.invoke(
        app,
        ["apply", "--state-dir", str(state), "--yes"],
        env=_env(metabase_url, metabase_api_key),
    )
    assert result.exit_code == 0, result.output
    assert (state / ".last-apply.json").exists()


def test_apply_backup_dir_creates_backup(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    backup = tmp_path / "backup"
    write_collection(state, "cli", unique_name("cli-backup"))
    result = runner.invoke(
        app,
        ["apply", "--state-dir", str(state), "--yes", "--backup-dir", str(backup)],
        env=_env(metabase_url, metabase_api_key),
    )
    assert result.exit_code == 0, result.output
    assert (backup / "collections").exists(), "backup export did not run before apply"


def test_init_scaffolds_files(tmp_path: Path) -> None:
    target = tmp_path / "scaffold"
    result = runner.invoke(
        app, ["init", str(target), "--url", "https://mb.example.com"]
    )
    assert result.exit_code == 0, result.output
    assert (target / ".env").exists()
    assert (target / ".gitignore").exists()
    assert (target / "README.md").exists()
    assert "https://mb.example.com" in (target / ".env").read_text()


def test_diagnose_redacts_key(
    metabase_url: str, metabase_api_key: str, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    result = runner.invoke(
        app,
        ["diagnose", "--state-dir", str(state)],
        env=_env(metabase_url, metabase_api_key),
    )
    assert result.exit_code == 0, result.output
    assert "redacted" in result.output.lower()
    assert metabase_api_key not in result.output
