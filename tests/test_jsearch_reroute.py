"""
JSearch surfaces a job via an aggregator (ZipRecruiter / LinkedIn) but often
ALSO lists the employer's real ATS link. We must prefer the ATS link so the
job routes to an applier that actually works (ZR native is Cloudflare-walled).
"""
from scrapers.jsearch import _pick_apply_link, _ats_for_url


def test_ats_for_url_detects_known_hosts():
    assert _ats_for_url("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert _ats_for_url("https://jobs.lever.co/acme/x") == "lever"
    assert _ats_for_url("https://acme.ashbyhq.com/x") == "ashby"
    assert _ats_for_url("https://acme.smartrecruiters.com/x") == "smartrecruiters"
    assert _ats_for_url("https://acme.wd1.myworkdayjobs.com/x") == "workday"
    assert _ats_for_url("https://www.ziprecruiter.com/jobs/x") is None
    assert _ats_for_url("") is None


def test_zr_listing_reroutes_to_greenhouse():
    job = {
        "job_apply_link": "https://www.ziprecruiter.com/jobs/abc",
        "job_publisher": "ZipRecruiter",
        "apply_options": [
            {"publisher": "ZipRecruiter", "apply_link": "https://www.ziprecruiter.com/jobs/abc"},
            {"publisher": "Greenhouse", "apply_link": "https://boards.greenhouse.io/acme/jobs/123"},
        ],
    }
    url, label = _pick_apply_link(job)
    assert label == "greenhouse"
    assert "greenhouse.io" in url


def test_direct_ats_primary_wins():
    job = {"job_apply_link": "https://jobs.lever.co/acme/xyz", "apply_options": []}
    url, label = _pick_apply_link(job)
    assert label == "lever"
    assert url == "https://jobs.lever.co/acme/xyz"


def test_aggregator_only_keeps_primary_and_no_ats():
    job = {
        "job_apply_link": "https://www.linkedin.com/jobs/view/999",
        "job_publisher": "LinkedIn",
        "apply_options": [],
    }
    url, label = _pick_apply_link(job)
    assert label is None
    assert url == "https://www.linkedin.com/jobs/view/999"


def test_google_link_fallback_when_no_apply_link():
    job = {"job_google_link": "https://www.google.com/search?q=job"}
    url, label = _pick_apply_link(job)
    assert label is None
    assert url == "https://www.google.com/search?q=job"
