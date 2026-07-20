"""Metro-area location expansion — browse + auto-apply parity."""
from locations import expand_location
from scheduler import _job_passes_saved_filters


def test_metro_expansion():
    la = expand_location("Los Angeles")
    assert "santa monica" in la and "irvine" in la and "burbank" in la
    assert expand_location("LA") == la          # alias
    assert expand_location("la") == la
    assert expand_location("SF") == expand_location("bay area")


def test_unknown_location_falls_back_to_substring():
    assert expand_location("Boise") == ["boise"]
    assert expand_location("") == []


def test_no_bare_la_false_positives():
    # "la" must never be a matching substring itself — it's inside "Atlanta".
    for pats in (expand_location("LA"), expand_location("los angeles")):
        assert "la" not in pats
        assert not any(p in "atlanta, ga" for p in pats)


def test_auto_apply_location_parity():
    job_sm = {"title": "Software Engineer", "company": "X",
              "location": "Santa Monica, CA", "description": ""}
    job_atl = {"title": "Software Engineer", "company": "X",
               "location": "Atlanta, GA", "description": ""}
    f = {"location": "Los Angeles"}
    assert _job_passes_saved_filters(job_sm, f)
    assert not _job_passes_saved_filters(job_atl, f)
    # remote special-case unchanged
    job_remote = {"title": "SWE", "company": "X", "location": "Remote", "description": ""}
    assert _job_passes_saved_filters(job_remote, {"location": "remote"})
