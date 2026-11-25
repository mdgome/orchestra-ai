"""Design 7.5 item 6 (a), the RECHECK cooldown through the real options computation on the embedded PostgreSQL: the first value
(60 s after ONE `REFUSED_ALIVE`), the doubling, the 900 s cap, and the reset by any other outcome. `k` of the contract counts the
`REFUSED_ALIVE` rechecks IMMEDIATELY PRECEDING the latest one, so n consecutive refusals give `60 * 2^(n-1)`."""
from datetime import UTC, datetime

import pytest

from app.validation import policies as P
from tests.integration_pg.test_risk_options import insert_request, options_of, scene, u_ids


async def hidden_entry(db, gates, approval):
    result = await options_of(db, gates, approval)
    return next((h for h in result.options_hidden if h.option_id == "recheck_processes"), None), result


@pytest.mark.parametrize("refusals,expected", [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (6, 900), (9, 900)])
async def test_the_cooldown_starts_at_60_doubles_and_is_capped_at_900(db, refusals, expected):
    assert (P.PROCESS_RECHECK_COOLDOWN_BASE_SECONDS, P.PROCESS_RECHECK_COOLDOWN_CAP_SECONDS) == (60, 900)
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    for i in range(refusals):                                                   # the older ones are outside the rolling window
        newest = i == refusals - 1
        await insert_request(db, project, task, kind="RECHECK", status="REFUSED_ALIVE", targets=rows,
                             finished_ago=10 if newest else 7200 + (refusals - i) * 100,
                             created_ago=0 if newest else 7200 + (refusals - i) * 100 + 5)
    entry, _ = await hidden_entry(db, gates, approval)
    assert entry is not None and entry.reason_code == "COOLDOWN" and entry.retryable is True
    remaining = (entry.next_available_at - datetime.now(UTC)).total_seconds()
    assert abs(remaining - (expected - 10)) < 5, (refusals, remaining)          # measured from the latest finished_at


async def test_any_other_outcome_resets_the_exponent(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    for i in range(3):                                                          # three old refusals ...
        await insert_request(db, project, task, kind="RECHECK", status="REFUSED_ALIVE", targets=rows,
                             finished_ago=9000 + i, created_ago=9100 + i)
    await insert_request(db, project, task, kind="RECHECK", status="NEEDS_REDECISION", targets=rows,
                         finished_ago=8000, created_ago=8100, reason="NO_LIVE_PROCESS_UNPROVEN")   # ... then another outcome
    await insert_request(db, project, task, kind="RECHECK", status="REFUSED_ALIVE", targets=rows, finished_ago=10)
    entry, _ = await hidden_entry(db, gates, approval)
    remaining = (entry.next_available_at - datetime.now(UTC)).total_seconds()
    assert entry.reason_code == "COOLDOWN" and abs(remaining - (60 - 10)) < 5   # a streak of one again: the first value
