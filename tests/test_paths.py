from pathlib import Path

from metabase_sync.serialize.paths import CollectionPaths, disambiguate, slugify


def test_slugify_basic():
    assert slugify("BigQuery Usage V3") == "bigquery-usage-v3"
    assert slugify("Tags / Compare") == "tags-compare"
    assert slugify("____") == "unnamed"
    assert slugify("  -- weird !! chars --  ") == "weird-chars"


def test_slugify_truncates_to_80_chars():
    long_name = "x" * 120
    out = slugify(long_name)
    assert len(out) <= 80


def test_disambiguate_no_collision():
    assert disambiguate("foo", "abcdef", set()) == "foo"


def test_disambiguate_with_collision():
    out = disambiguate("foo", "ABCDEF12345", {"foo"})
    assert out.startswith("foo-")
    assert out != "foo"


def test_collection_paths_builds_nested_tree(tmp_path: Path):
    paths = CollectionPaths(tmp_path)
    paths.add(1, None, "Monitoring", "ent1")
    paths.add(2, 1, "BigQuery Usage", "ent2")
    paths.add(3, 2, "V3", "ent3")

    assert paths.directory_for(1) == tmp_path / "collections" / "monitoring"
    assert (
        paths.directory_for(2)
        == tmp_path / "collections" / "monitoring" / "bigquery-usage"
    )
    assert (
        paths.directory_for(3)
        == tmp_path / "collections" / "monitoring" / "bigquery-usage" / "v3"
    )


def test_collection_paths_disambiguates_siblings(tmp_path: Path):
    paths = CollectionPaths(tmp_path)
    paths.add(1, None, "Monitoring", "ent1")
    # two children with the same name
    paths.add(2, 1, "X", "ent2_aaaaaa")
    paths.add(3, 1, "X", "ent3_bbbbbb")
    assert paths.directory_for(2).name == "x"
    assert paths.directory_for(3).name != "x"
