"""Applies wait while a scrape runs in the same process; SQL helpers for the fresh/US feed."""
import asyncio

import scheduler
from api.routes.queue import _wait_for_scrape_to_finish
from db import fresh_job_sql, us_region_sql


def test_no_wait_without_a_scrape():
    scheduler._SCRAPES_RUNNING = 0
    assert not scheduler.scrape_in_progress()
    asyncio.run(asyncio.wait_for(_wait_for_scrape_to_finish(), timeout=1))


def test_wait_is_bounded():
    scheduler._SCRAPES_RUNNING = 1
    try:
        assert scheduler.scrape_in_progress()
        asyncio.run(asyncio.wait_for(_wait_for_scrape_to_finish(max_wait_s=0), timeout=1))
    finally:
        scheduler._SCRAPES_RUNNING = 0


def test_region_sql_strict_excludes_unclassified():
    assert us_region_sql("j") == "COALESCE(j.region, 'unknown') IN ('us', 'unknown')"
    assert us_region_sql("j", strict=True) == "COALESCE(j.region, 'pending') IN ('us', 'unknown')"


def test_fresh_sql_uses_both_windows():
    sql = fresh_job_sql("j")
    assert "COALESCE(j.last_seen_at, j.created_at)" in sql and "j.created_at >" in sql
