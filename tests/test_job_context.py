"""
Job-aware answer tailoring must never leak across companies:
- textarea answers (cover letters, motivation) cache PER JOB
- factual answers (EEOC, dropdowns) stay globally cached (cost)
- the context var is task-local and clears cleanly
"""
from applier.greenhouse import (
    build_job_context, set_job_context, _job_ctx, _cache_basis,
    _cache_get, _cache_set,
)

JOB_A = {"title": "Backend Engineer", "company": "Stripe",
         "location": "Remote", "description": "<p>Build payment APIs with Go & Kubernetes</p>"}
JOB_B = {"title": "Backend Engineer", "company": "Reddit",
         "location": "Remote", "description": "<p>Scale feed infrastructure</p>"}
PROFILE = "Name: Houman\nSkills: Python, Go"


def teardown_function(_):
    set_job_context(None)


def test_build_job_context_strips_html_and_includes_identity():
    ctx = build_job_context(JOB_A)
    assert "Stripe" in ctx and "Backend Engineer" in ctx
    assert "payment APIs" in ctx
    assert "<p>" not in ctx  # html stripped


def test_set_and_clear_context():
    set_job_context(JOB_A)
    assert "Stripe" in _job_ctx.get()
    set_job_context(None)
    assert _job_ctx.get() == ""


def test_textarea_cache_is_per_job():
    basis_a = _cache_basis(PROFILE, "textarea", build_job_context(JOB_A))
    basis_b = _cache_basis(PROFILE, "textarea", build_job_context(JOB_B))
    assert basis_a != basis_b
    q = "Write a brief cover letter for this position"
    _cache_set(q, "textarea", basis_a, "letter about Stripe")
    # Job B must NOT get Stripe's letter
    assert _cache_get(q, "textarea", basis_b) is None
    assert _cache_get(q, "textarea", basis_a) == "letter about Stripe"


def test_factual_cache_stays_global_across_jobs():
    # dropdown/EEOC answers are job-independent — same basis regardless of job
    assert _cache_basis(PROFILE, "dropdown", build_job_context(JOB_A)) == PROFILE
    assert _cache_basis(PROFILE, "dropdown", build_job_context(JOB_B)) == PROFILE
    # and textarea WITHOUT a job context also stays global (legacy behavior)
    assert _cache_basis(PROFILE, "textarea", "") == PROFILE
