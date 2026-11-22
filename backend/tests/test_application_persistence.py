"""在线启动只检查 Alembic 版本，不执行建库、DDL、种子或历史修复。"""

from contextlib import contextmanager

import pytest

from app.application import persistence


@pytest.fixture
def initialization_steps(monkeypatch):
    steps = []
    connection = object()

    class Engine:
        @contextmanager
        def connect(self):
            steps.append("connect")
            yield connection
            steps.append("close")

    def check(value):
        assert value is connection
        steps.append("check_revision")

    monkeypatch.setattr(persistence, "engine", Engine)
    monkeypatch.setattr(persistence, "assert_schema_current", check)
    return steps


def test_initialize_database_only_checks_revision(initialization_steps):
    persistence.initialize_database()
    assert initialization_steps == ["connect", "check_revision", "close"]


def test_missing_revision_fails_without_automatic_upgrade(initialization_steps, monkeypatch):
    def failed_check(connection):
        raise RuntimeError("业务数据库结构未升级")

    monkeypatch.setattr(persistence, "assert_schema_current", failed_check)
    with pytest.raises(RuntimeError, match="未升级"):
        persistence.initialize_database()
    assert initialization_steps == ["connect"]
