import pytest

from metabase_sync.store import yamlio
from metabase_sync.store.tree import read_tree

from ..test_cli import run
from ._content import unique
from .test_roundtrip import export

pytestmark = pytest.mark.integration


@pytest.fixture
def connected(metabase_url_and_key, monkeypatch):
    url, key = metabase_url_and_key
    monkeypatch.setenv("METABASE_URL", url)
    monkeypatch.setenv("METABASE_API_KEY", key)


def test_init_finds_a_collection_by_name(client, mb, tmp_path, monkeypatch, connected):
    parent = mb.create_collection(unique("Finance"))
    child = mb.create_collection(unique("Monthly"), parent_id=parent["id"])
    card = mb.create_native_card("Revenue", "SELECT 1", collection_id=child["id"])

    code = run(
        monkeypatch,
        "init",
        "--chdir",
        str(tmp_path),
        "--collection",
        child["name"].upper(),
    )
    export(client, tmp_path)

    assert code == 0
    assert yamlio.read(tmp_path / "metabase-sync.yaml")["collections"] == [
        child["entity_id"]
    ]
    assert (
        f"# {parent['name']} / {child['name']}"
        in (tmp_path / "metabase-sync.yaml").read_text()
    )
    assert card["entity_id"] in read_tree(tmp_path).entities


def test_init_refuses_a_name_shared_by_two_collections(
    mb, tmp_path, monkeypatch, capsys, connected
):
    name = unique("Monthly")
    first = mb.create_collection(
        name, parent_id=mb.create_collection(unique("A"))["id"]
    )
    second = mb.create_collection(
        name, parent_id=mb.create_collection(unique("B"))["id"]
    )

    code = run(monkeypatch, "init", "--chdir", str(tmp_path), "--collection", name)

    printed = capsys.readouterr().err
    assert code == 1
    assert first["entity_id"] in printed and second["entity_id"] in printed
    assert not (tmp_path / "metabase-sync.yaml").exists()


def test_init_prints_the_lines_to_add_when_the_config_exists(
    mb, tmp_path, monkeypatch, capsys, connected
):
    collection = mb.create_collection(unique("Finance"))
    root = tmp_path / "[team]"
    root.mkdir()
    (root / "metabase-sync.yaml").write_text("# mine\nremoval: delete\n")

    code = run(
        monkeypatch,
        "init",
        "--chdir",
        str(root),
        "--collection",
        collection["entity_id"],
        "--collection",
        collection["name"],
    )

    printed = capsys.readouterr().err
    assert code == 0
    assert (root / "metabase-sync.yaml").read_text() == "# mine\nremoval: delete\n"
    assert f"{root}/metabase-sync.yaml already exists" in printed
    assert printed.count(f"- {collection['entity_id']}  # {collection['name']}") == 1


@pytest.mark.parametrize("field", ["entity_id", "name"])
def test_init_explains_that_a_personal_collection_cannot_be_managed(
    mb, tmp_path, monkeypatch, capsys, connected, field
):
    personal = mb.get(f"/api/collection/{mb.admin_user()['personal_collection_id']}")

    code = run(
        monkeypatch,
        "init",
        "--chdir",
        str(tmp_path),
        "--collection",
        personal[field],
    )

    assert code == 1
    assert "personal or system collection" in capsys.readouterr().err
