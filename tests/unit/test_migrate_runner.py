import hashlib
from pathlib import Path

import pytest

from polyagent.scripts.migrate import (
    Migration,
    discover_migrations,
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
