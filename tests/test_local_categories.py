"""
Local (warehouse/temp) categories must never disturb the professional path:
- default behavior (software only) is byte-identical
- local queries are searched with the user's area, never "remote"
- local categories are skipped entirely when no area is configured
- bare "warehouse" must NOT be a title word (would match Data Warehouse Engineer)
"""
import job_categories as jc
from matcher import is_engineering_job


def setup_function(_):
    jc.set_active(None)      # reset to default
    jc.set_local_area("")


def teardown_function(_):
    jc.set_active(None)
    jc.set_local_area("")


def test_default_specs_unchanged_software_only():
    specs = jc.active_query_specs()
    assert all(s["local"] is False for s in specs)
    assert [s["query"] for s in specs] == jc.JOB_CATEGORIES["software_engineering"]["queries"]


def test_local_category_skipped_without_area():
    jc.set_active(["software_engineering", "warehouse_logistics"])
    specs = jc.active_query_specs()
    # no area set -> warehouse queries must NOT appear (searching nationwide
    # for in-person jobs is worse than not searching)
    assert all(s["category"] != "warehouse_logistics" for s in specs)
    # active_queries() (used by non-local-aware scrapers) also excludes them
    assert "warehouse associate" not in jc.active_queries()


def test_local_category_included_with_area():
    jc.set_active(["software_engineering", "warehouse_logistics"])
    jc.set_local_area("Santa Ana, CA")
    specs = jc.active_query_specs()
    wh = [s for s in specs if s["category"] == "warehouse_logistics"]
    sw = [s for s in specs if s["category"] == "software_engineering"]
    assert wh and sw
    assert all(s["local"] for s in wh)
    assert all(not s["local"] for s in sw)
    assert jc.local_area() == "Santa Ana, CA"


def test_data_warehouse_engineer_is_not_a_warehouse_job():
    # Bare "warehouse" must not be in the local title words — it would
    # classify tech jobs as warehouse work.
    words = jc.JOB_CATEGORIES["warehouse_logistics"]["title_words"]
    assert "warehouse" not in words
    # And with BOTH categories active, a Data Warehouse Engineer title is
    # kept by the title gate via the ENGINEERING words (not misfiled).
    jc.set_active(["software_engineering", "warehouse_logistics"])
    assert is_engineering_job("Senior Data Warehouse Engineer")


def test_warehouse_titles_pass_gate_only_when_category_active():
    assert not is_engineering_job("Warehouse Associate")  # default: SWE only
    jc.set_active(["warehouse_logistics"])
    assert is_engineering_job("Warehouse Associate")
    assert is_engineering_job("Forklift Operator - Night Shift")
    assert is_engineering_job("Order Picker / Packer")


def test_local_keys():
    assert jc.local_keys() == {"warehouse_logistics"}
