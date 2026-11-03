import pytest
from pydantic import ValidationError

import app.db as app_db
from app.config import Settings


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_a_total_capacity_of_one_is_rejected_and_two_is_allowed():
    with pytest.raises(ValidationError):
        make(db_pool_size=1, db_max_overflow=0)
    assert make(db_pool_size=1, db_max_overflow=1).db_max_overflow == 1
    assert make(db_pool_size=2, db_max_overflow=0).effective_db_pool_size == 2     # fixed pool, overflow 0


def test_invalid_pool_values_are_rejected():
    for bad in ({"db_pool_size": 0}, {"db_max_overflow": -1}, {"max_concurrent_tasks": 0},
                {"max_tasks_per_project": 0}, {"max_concurrent_cli_processes": 0}):
        with pytest.raises(ValidationError):
            make(**bad)


def test_an_explicit_capacity_below_the_recommendation_is_accepted():
    s = make(max_concurrent_tasks=8, db_pool_size=3, db_max_overflow=0)           # recommendation would be 38
    assert s.effective_db_pool_size == 3


def test_unset_pool_follows_the_recommended_formula_and_an_explicit_one_does_not_move():
    assert make().effective_db_pool_size == 14                                    # 4 * 2 + 6
    assert make(max_concurrent_tasks=5).effective_db_pool_size == 26
    assert make(max_concurrent_tasks=5, db_pool_size=7).effective_db_pool_size == 7


def test_create_engine_passes_the_pool_arguments(monkeypatch):
    seen = {}
    monkeypatch.setattr(app_db, "create_async_engine", lambda url, **kw: seen.update(url=url, **kw) or object())
    app_db.create_engine("postgresql+asyncpg://x/y", 3, 0)
    assert (seen["pool_size"], seen["max_overflow"]) == (3, 0)
    app_db.create_engine("postgresql+asyncpg://x/y")                              # the old call keeps working
    assert (seen["pool_size"], seen["max_overflow"]) == (10, 10)
