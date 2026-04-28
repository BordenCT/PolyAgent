import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from polyagent.scripts.migrate import (
    AppliedRecord,
    DriftError,
    Migration,
    discover_migrations,
    plan_actions,
)


def test_discover_migrations_returns_lex_sorted_with_versions(tmp_path):
    (tmp_path / "002_second.sql").write_text("SELECT 2;")
    (tmp_path / "001_first.sql").write_text("SELECT 1;")
    (tmp_path / "010_tenth.sql").write_text("SELECT 10;")
    (tmp_path / "README.md").write_text("ignored")

    found = discover_migrations(tmp_path)

    assert [m.version for m in found] == ["001", "002", "010"]
    assert [m.filename for m in found] == [
        "001_first.sql", "002_second.sql", "010_tenth.sql",
    ]
    assert found[0].sql == "SELECT 1;"
    assert found[0].checksum == hashlib.sha256(b"SELECT 1;").hexdigest()


def test_discover_migrations_empty_dir_returns_empty(tmp_path):
    assert discover_migrations(tmp_path) == []


def test_discover_migrations_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        discover_migrations(tmp_path / "missing")


def _ar(version: str, checksum: str) -> AppliedRecord:
    return AppliedRecord(
        version=version, filename=f"{version}.sql", checksum=checksum,
        applied_at=datetime(2026, 1, 1),
    )


def _mig(version: str, checksum: str) -> Migration:
    return Migration(version=version, filename=f"{version}.sql", sql="SELECT 1;", checksum=checksum)


def test_plan_actions_skips_matching_checksums():
    found = [_mig("001", "a"), _mig("002", "b")]
    applied = {"001": _ar("001", "a")}
    pending = plan_actions(found, applied)
    assert [m.version for m in pending] == ["002"]


def test_plan_actions_raises_on_drift():
    found = [_mig("001", "a")]
    applied = {"001": _ar("001", "DIFFERENT")}
    with pytest.raises(DriftError) as ei:
        plan_actions(found, applied)
    assert "001" in str(ei.value)


def test_plan_actions_empty_when_all_applied():
    found = [_mig("001", "a"), _mig("002", "b")]
    applied = {"001": _ar("001", "a"), "002": _ar("002", "b")}
    assert plan_actions(found, applied) == []
