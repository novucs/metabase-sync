"""Runtime Metabase version check: warn outside our tested band, refuse on
versions so old the API shape we depend on doesn't exist."""

from __future__ import annotations

import pytest

from metabase_sync.apply._runner import (
    UnsupportedMetabaseVersion,
    _check_metabase_version,
    _parse_version,
)


class _FakeClient:
    def __init__(self, version_tag: str | None) -> None:
        self._version_tag = version_tag

    def get(self, _path: str, **_params):
        if self._version_tag is None:
            return {}
        return {"version": {"tag": self._version_tag}}


def test_parse_version_normal():
    assert _parse_version("v0.62.2") == (0, 62)
    assert _parse_version("0.55.10.4") == (0, 55)
    assert _parse_version("v1.0.0") == (1, 0)


def test_parse_version_unparseable():
    assert _parse_version("nightly") is None
    assert _parse_version("") is None


def test_hard_floor_refuses(caplog):
    with pytest.raises(UnsupportedMetabaseVersion, match="hard floor"):
        _check_metabase_version(_FakeClient("v0.40.0"))  # type: ignore[arg-type]


def test_tested_floor_warns(caplog):
    with caplog.at_level("WARNING"):
        _check_metabase_version(_FakeClient("v0.50.0"))  # type: ignore[arg-type]
    assert any("tested floor" in rec.message for rec in caplog.records)


def test_tested_ceiling_warns(caplog):
    with caplog.at_level("WARNING"):
        _check_metabase_version(_FakeClient("v0.99.0"))  # type: ignore[arg-type]
    assert any("tested ceiling" in rec.message for rec in caplog.records)


def test_inside_tested_band_silent(caplog):
    with caplog.at_level("WARNING"):
        _check_metabase_version(_FakeClient("v0.62.2"))  # type: ignore[arg-type]
    assert not any("Metabase" in rec.message for rec in caplog.records)


def test_unparseable_tag_logs_but_continues(caplog):
    with caplog.at_level("WARNING"):
        _check_metabase_version(_FakeClient("custom-snapshot"))  # type: ignore[arg-type]
    assert any("not in major.minor" in rec.message for rec in caplog.records)


def test_endpoint_failure_does_not_block(caplog):
    """If /api/session/properties fails entirely (e.g. the instance is
    so old the endpoint isn't there), we just log and continue — we don't
    want the version check itself to be load-bearing."""

    class _ExplodingClient:
        def get(self, _path, **_params):
            raise RuntimeError("boom")

    with caplog.at_level("WARNING"):
        _check_metabase_version(_ExplodingClient())  # type: ignore[arg-type]
    assert any("could not read" in rec.message for rec in caplog.records)
