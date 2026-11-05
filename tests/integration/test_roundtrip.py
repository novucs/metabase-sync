import subprocess
from pathlib import Path

import pytest

from metabase_sync.store import yamlio
from metabase_sync.sync import commands as workflow
from metabase_sync.sync.plan import Plan

from ..pins import REPRESENTATIONS
from ._content import build_rich

pytestmark = pytest.mark.integration


def scope(root: Path, collection_eid: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    yamlio.write(root / "metabase-sync.yaml", {"collections": [collection_eid]})
    return root


def describe(plan: Plan) -> list[tuple[str, str, str, list[str]]]:
    return [(op.action, op.kind, op.name, op.changes) for op in plan.operations]


def test_export_then_plan_is_empty(client, mb, tmp_path):
    rich = build_rich(mb)
    root = scope(tmp_path / "root", rich.collection["entity_id"])

    result = workflow.export(client, root)
    _, plan = workflow.plan(client, root)

    assert len(result.added) >= 9
    assert plan.empty, describe(plan)
    assert workflow.export(client, root, check=True).clean


def test_exported_files_pass_metabases_representation_validator(client, mb, tmp_path):
    rich = build_rich(mb)
    root = scope(tmp_path / "root", rich.collection["entity_id"])
    workflow.export(client, root)

    validated = subprocess.run(
        [
            "npx",
            "--yes",
            f"@metabase/representations@{REPRESENTATIONS}",
            "validate-schema",
            "--folder",
            str(root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert validated.returncode == 0, validated.stdout + validated.stderr
