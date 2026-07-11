"""Auto-Apply role targeting: title-only, empty = unrestricted."""
from scheduler import _job_passes_saved_filters

SWE = {"title": "Senior Software Engineer, Payments", "company": "Stripe",
       "location": "Remote", "description": "we use devops practices daily"}
SD  = {"title": "Software Developer II", "company": "Acme", "location": "", "description": ""}
DEVOPS = {"title": "DevOps Engineer", "company": "Acme", "location": "", "description": ""}


def test_empty_roles_is_unrestricted():
    assert _job_passes_saved_filters(SWE, {"title_roles": []})
    assert _job_passes_saved_filters(SWE, {})


def test_title_only_matching():
    f = {"title_roles": ["software developer", "devops"]}
    assert _job_passes_saved_filters(SD, f)
    assert _job_passes_saved_filters(DEVOPS, f)
    # SWE mentions devops in the DESCRIPTION only — must NOT pass (that's
    # exactly the looseness that keywords have and this filter must not).
    assert not _job_passes_saved_filters(SWE, f)


def test_case_insensitive_and_whitespace():
    f = {"title_roles": ["  Software Engineer  "]}
    assert _job_passes_saved_filters(SWE, f)
